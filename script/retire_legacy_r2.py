#!/usr/bin/env python3
"""只清理已获授权的上下位机旧 R2 前缀，保留逐对象结果。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


ACCOUNT = "e552c257969e8aaf585436d74852dcc8"
BUCKET = "mskdsp-update"
PREFIXES = ("mskdsp-lower-x64/", "mskdsp-lower/", "mskdsp-upper/")


class RetirementError(ValueError):
    """可安全写入日志的清理失败原因。"""


def is_target(key):
    return isinstance(key, str) and key.startswith(PREFIXES)


def aws(operation, *arguments):
    environment = dict(os.environ)
    for name in ("R2_ACCOUNT_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        value = environment.get(name, "").strip()
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise RetirementError("R2 凭据为空或格式不合法")
        environment[name] = value
    if environment["R2_ACCOUNT_ID"] != ACCOUNT:
        raise RetirementError("R2 账号与已授权账号不一致")
    command = ["aws", "s3api", operation, "--bucket", BUCKET,
               "--endpoint-url", f"https://{ACCOUNT}.r2.cloudflarestorage.com",
               "--region", "auto", "--output", "json", "--no-cli-pager", *arguments]
    result = subprocess.run(command, env=environment, capture_output=True, text=True,
                            encoding="utf-8", timeout=90, check=False)
    if result.returncode:
        raise RetirementError("R2 API 操作失败：" + operation)
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise RetirementError("R2 API 响应不是合法 JSON") from None
    if not isinstance(data, dict):
        raise RetirementError("R2 API 响应不是对象")
    return data


def normalize(item):
    if (not isinstance(item, dict) or not isinstance(item.get("Key"), str)
            or not item["Key"] or type(item.get("Size")) is not int or item["Size"] < 0
            or not isinstance(item.get("ETag"), str) or not item["ETag"]
            or not isinstance(item.get("LastModified"), str) or not item["LastModified"]):
        raise RetirementError("R2 对象清单字段不完整")
    return {name: item[name] for name in ("Key", "Size", "ETag", "LastModified")}


def list_objects(api=aws):
    objects, tokens, token = {}, set(), None
    while True:
        arguments = ["--max-keys", "1000", "--no-paginate"]
        if token is not None:
            arguments += ["--continuation-token", token]
        page = api("list-objects-v2", *arguments)
        if type(page.get("IsTruncated")) is not bool or not isinstance(page.get("Contents", []), list):
            raise RetirementError("R2 分页列表格式不合法")
        for value in page.get("Contents", []):
            item = normalize(value)
            if item["Key"] in objects:
                raise RetirementError("R2 分页包含重复对象")
            objects[item["Key"]] = item
        if not page["IsTruncated"]:
            return [objects[key] for key in sorted(objects)]
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token or token in tokens:
            raise RetirementError("R2 分页令牌缺失或重复")
        tokens.add(token)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snapshot(directory, api=aws):
    objects = list_objects(api)
    inventory = {"account_id": ACCOUNT, "bucket": BUCKET, "prefixes": list(PREFIXES), "objects": objects}
    path = Path(directory) / "inventory.json"
    write_json(path, inventory)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (Path(directory) / "inventory.sha256").write_text(digest + "\n", encoding="ascii")
    for prefix in PREFIXES:
        items = [item for item in objects if item["Key"].startswith(prefix)]
        print(f"待清理前缀：{prefix}，对象={len(items)}，字节={sum(item['Size'] for item in items)}")
    print(f"非目标对象保留：{sum(not is_target(item['Key']) for item in objects)}")
    print("清单 SHA256：" + digest)
    return inventory


def load_inventory(directory):
    path = Path(directory) / "inventory.json"
    expected = (Path(directory) / "inventory.sha256").read_text(encoding="ascii").strip()
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RetirementError("冻结清单摘要不一致")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("account_id") != ACCOUNT or data.get("bucket") != BUCKET
            or data.get("prefixes") != list(PREFIXES) or not isinstance(data.get("objects"), list)):
        raise RetirementError("冻结清单越出授权范围")
    items = [normalize(item) for item in data["objects"]]
    if len({item["Key"] for item in items}) != len(items):
        raise RetirementError("冻结清单包含重复对象")
    return sorted(items, key=lambda item: item["Key"])


def apply(directory, api=aws):
    result = {"bucket": BUCKET, "prefixes": list(PREFIXES), "status": "running", "operations": []}
    result_path = Path(directory) / "result.json"
    write_json(result_path, result)
    try:
        before = load_inventory(directory)
        if list_objects(api) != before:
            raise RetirementError("删除前存储桶已变化，未开始删除")
        for item in before:
            key = item["Key"]
            if not is_target(key):
                continue
            operation = {"key": key, "status": "checking"}
            result["operations"].append(operation)
            write_json(result_path, result)
            head = api("head-object", "--key", key)
            if head.get("ContentLength") != item["Size"] or head.get("ETag") != item["ETag"]:
                raise RetirementError("对象大小或 ETag 已变化，停止删除：" + key)
            operation["status"] = "deleting"
            write_json(result_path, result)
            api("delete-object", "--key", key)
            operation["status"] = "delete_acknowledged"
            write_json(result_path, result)
            print("已删除旧 R2 对象：" + key, flush=True)
        after = list_objects(api)
        write_json(Path(directory) / "after.json", {"objects": after})
        if any(is_target(item["Key"]) for item in after):
            raise RetirementError("清理后仍有目标对象，未宣称完成")
        if after != [item for item in before if not is_target(item["Key"])]:
            raise RetirementError("非目标对象发生变化，需要检查并发任务")
        for operation in result["operations"]:
            operation["status"] = "verified_absent"
        result.update(status="success", deleted=len(result["operations"]), preserved=len(after))
        print(f"清理完成：删除={result['deleted']}，非目标保留={result['preserved']}", flush=True)
    except Exception as error:
        result["status"] = "failed"
        result["error"] = str(error) if isinstance(error, RetirementError) else "清理异常，需检查运行日志和结果"
        raise
    finally:
        write_json(result_path, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "apply"))
    parser.add_argument("--directory", default="package/r2-retirement")
    args = parser.parse_args()
    try:
        (snapshot if args.action == "snapshot" else apply)(args.directory)
    except Exception as error:
        message = str(error) if isinstance(error, RetirementError) else "清理异常，未确认全部完成"
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()

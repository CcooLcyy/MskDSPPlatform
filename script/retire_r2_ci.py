#!/usr/bin/env python3
"""分两阶段备份并退役已确认的五个旧 R2 CI 对象；不执行安装包。"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PREFIX = "mskdsp-platform/ci/"
PUBLIC_BASE = "https://update.clsclear.top"
TAG = "0.0.1-ci.20261018t123737.8.110-sha.61024ff"
VERSION = "0.0.1-ci.20261018t123737.8.110+sha.61024ff"
COMMIT = "61024ff8efb79b73b74a68c654f84eccdfe2d43b"
BUILD_TIME = "2026-10-18T12:37:37Z"
VERSION_ROOT = PREFIX + TAG + "/"
LATEST_KEY = PREFIX + "latest.json"
RELEASE_KEY = VERSION_ROOT + "release.json"
SUMS_KEY = VERSION_ROOT + "SHA256SUMS"
IMAGE_KEY = VERSION_ROOT + "linux-amd64/mskdsp-platform-" + TAG + "-linux-amd64-image.tar.gz"
BINARY_KEY = VERSION_ROOT + "linux-amd64/mskdsp-platform-" + TAG + "-linux-amd64-binary.tar.gz"
KEYS = (LATEST_KEY, RELEASE_KEY, SUMS_KEY, IMAGE_KEY, BINARY_KEY)
PACKAGE_IDENTITIES = {
    IMAGE_KEY: {"size": 49409069, "sha256": "9d9352daff66ac827d437b28a0041226fecad14b67acd65c0126b625f08a24ab"},
    BINARY_KEY: {"size": 3602738, "sha256": "8f5f9a45838dadf64a14b5ddb9f1dbabb8e9a387c99295e6289574337c05c1e9"},
}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class RetirementError(ValueError):
    """仅保存固定中文诊断，不包含云端或子进程原始输出。"""


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_bucket(bucket):
    if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
        raise RetirementError("存储桶必须为单个合法名称")
    return bucket


def regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RetirementError("备份对象缺失或不是普通文件")
    for parent in path.parents:
        if parent.is_symlink():
            raise RetirementError("备份路径不能包含符号链接")
    return path


def write_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise RetirementError("输出文件不能为符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in path.parents:
        if parent.is_symlink():
            raise RetirementError("输出目录不能包含符号链接")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_small_json(path):
    path = regular(path)
    if path.stat().st_size > 1024 * 1024:
        raise RetirementError("清单或盘点文件超过大小限制")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise RetirementError("清单或盘点文件不是有效 JSON") from None


def object_metadata(item, *, head=False, key=None):
    try:
        name = key if head else item["Key"]
        size = item["ContentLength"] if head else item["Size"]
        etag, modified = item["ETag"], item["LastModified"]
        if (name not in KEYS or type(size) is not int or size <= 0
                or not isinstance(etag, str) or not re.fullmatch(r'"[0-9a-fA-F]{32}(?:-[1-9][0-9]*)?"', etag)
                or not isinstance(modified, str) or datetime.fromisoformat(modified.replace("Z", "+00:00")).tzinfo is None):
            raise ValueError()
        if name in PACKAGE_IDENTITIES and size != PACKAGE_IDENTITIES[name]["size"]:
            raise ValueError()
        if name not in PACKAGE_IDENTITIES and size > 1024 * 1024:
            raise ValueError()
        return dict(key=name, size=size, etag=etag, last_modified=modified)
    except (KeyError, TypeError, ValueError):
        raise RetirementError("远端对象键、大小、摘要标识或时间不符合已确认范围") from None


def list_objects(store, *, allow_empty=False):
    found, seen_tokens, token = {}, set(), None
    while True:
        page = store.list_page(token)
        if not isinstance(page, dict) or type(page.get("IsTruncated")) is not bool or not isinstance(page.get("Contents", []), list):
            raise RetirementError("对象列表格式不合法")
        for raw in page.get("Contents", []):
            item = object_metadata(raw)
            if item["key"] in found:
                raise RetirementError("对象列表包含重复键，拒绝继续")
            found[item["key"]] = item
        if not page["IsTruncated"]:
            break
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise RetirementError("对象列表分页异常，拒绝继续")
        seen_tokens.add(token)
    if (not found and allow_empty) or set(found) == set(KEYS):
        return found
    raise RetirementError("CI 前缀对象必须恰好为已确认的五个对象")


def validate_backup(root, objects):
    if len(objects) != 5 or {item.get("key") for item in objects} != set(KEYS):
        raise RetirementError("备份盘点范围不正确")
    paths = {}
    for item in objects:
        key = item["key"]
        if item.get("backup_path") != "objects/" + key or type(item.get("size")) is not int or not HEX64.fullmatch(item.get("sha256", "")):
            raise RetirementError("备份路径、大小或摘要格式不合法")
        path = regular(Path(root) / item["backup_path"])
        if path.stat().st_size != item["size"] or file_sha256(path) != item["sha256"]:
            raise RetirementError("备份文件大小或摘要与盘点不符")
        if key in PACKAGE_IDENTITIES and any(item[field] != PACKAGE_IDENTITIES[key][field] for field in ("size", "sha256")):
            raise RetirementError("程序包或镜像包与已确认身份不符")
        paths[key] = path
    latest, release = read_small_json(paths[LATEST_KEY]), read_small_json(paths[RELEASE_KEY])
    expected = dict(schema_version=1, project="mskdsp-platform", channel="ci", base_version="0.0.1",
                    version=VERSION, commit=COMMIT, build_time=BUILD_TIME, published_at=BUILD_TIME)
    if not isinstance(latest, dict) or latest != release or any(latest.get(key) != value for key, value in expected.items()):
        raise RetirementError("备份的版本清单和入口身份不一致")
    platforms = latest.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != {"linux-amd64"}:
        raise RetirementError("备份清单架构不正确")
    entry = platforms["linux-amd64"]
    if not isinstance(entry, dict) or entry.get("image") != "ghcr.io/ccoolcyy/mskdsp-platform:" + TAG or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.get("image_id", "")):
        raise RetirementError("备份清单镜像身份不正确")
    for marker, key in (("", IMAGE_KEY), ("binary_", BINARY_KEY)):
        if (entry.get(marker + "url") != PUBLIC_BASE + "/" + key
                or entry.get(marker + "size") != PACKAGE_IDENTITIES[key]["size"]
                or entry.get(marker + "sha256") != PACKAGE_IDENTITIES[key]["sha256"]):
            raise RetirementError("清单引用与已确认对象身份不一致")
    expected_sums = "".join(PACKAGE_IDENTITIES[key]["sha256"] + "  linux-amd64/" + key.rsplit("/", 1)[1] + "\n"
                            for key in (IMAGE_KEY, BINARY_KEY))
    if paths[SUMS_KEY].read_text(encoding="utf-8") != expected_sums:
        raise RetirementError("校验和文件与备份清单不一致")


class AwsStore:
    """云端调用仅使用参数数组和当前进程环境，底层输出全部截获。"""
    def __init__(self, bucket):
        self.bucket = valid_bucket(bucket)
        self.environment = dict(os.environ)
        for name in ("R2_ACCOUNT_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            value = self.environment.get(name, "").strip()
            if not value or any(char.isspace() or ord(char) < 32 for char in value):
                raise RetirementError("R2 只读或清理凭据未配置或格式不合法")
            self.environment[name] = value
        if not re.fullmatch(r"[0-9a-f]{32}", self.environment["R2_ACCOUNT_ID"]):
            raise RetirementError("R2 账号格式不合法")
        self.endpoint = "https://" + self.environment["R2_ACCOUNT_ID"] + ".r2.cloudflarestorage.com"
        self.environment.update(AWS_DEFAULT_REGION="auto", AWS_REQUEST_CHECKSUM_CALCULATION="when_required",
                                AWS_RESPONSE_CHECKSUM_VALIDATION="when_required", AWS_PAGER="")

    def command(self, *arguments):
        argv = ["aws", "s3api", *arguments, "--bucket", self.bucket, "--endpoint-url", self.endpoint,
                "--region", "auto", "--output", "json"]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", check=True,
                                    env=self.environment, timeout=180)
            return json.loads(result.stdout or "{}")
        except (OSError, ValueError, subprocess.SubprocessError):
            raise RetirementError("R2 操作失败，已隐藏命令和底层输出") from None

    def list_page(self, token=None):
        arguments = ["list-objects-v2", "--prefix", PREFIX, "--no-paginate"]
        if token is not None:
            arguments += ["--continuation-token", token]
        return self.command(*arguments)

    def download(self, key, destination):
        if key not in KEYS:
            raise RetirementError("下载对象越出已确认范围")
        return self.command("get-object", "--key", key, str(destination))

    def head(self, key):
        if key not in KEYS:
            raise RetirementError("查询对象越出已确认范围")
        return self.command("head-object", "--key", key)

    def delete(self, key):
        if key not in KEYS:
            raise RetirementError("删除对象越出已确认范围")
        return self.command("delete-object", "--key", key)


def snapshot(store, output, *, bucket):
    bucket = valid_bucket(bucket)
    remote = list_objects(store)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise RetirementError("快照目录已存在，请使用独立新目录")
    output.mkdir(parents=True)
    objects = []
    for key in KEYS:
        relative = "objects/" + key
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = store.download(key, destination)
        item = remote[key]
        if not isinstance(response, dict) or response.get("ETag") != item["etag"]:
            raise RetirementError("下载响应的对象标识与列举不一致")
        if object_metadata(store.head(key), head=True, key=key) != item:
            raise RetirementError("备份期间远端对象发生变化")
        objects.append(dict(item, sha256=file_sha256(regular(destination)), backup_path=relative))
    validate_backup(output, objects)
    if list_objects(store) != remote:
        raise RetirementError("备份完成前远端对象发生变化")
    inventory = dict(schema_version=1, bucket=bucket, prefix=PREFIX, version=VERSION, commit=COMMIT,
                     object_count=5, total_size=sum(item["size"] for item in objects), objects=objects)
    write_json(output / "inventory.json", inventory)
    return inventory


def apply(store, snapshot_dir, inventory_sha256, report_path, *, bucket):
    report = dict(schema_version=1, status="checking", prefix=PREFIX, deleted_keys=[], uncertain_keys=[],
                  stage="校验本地快照", failed_key="")
    root = Path(snapshot_dir)
    if Path(report_path).resolve().is_relative_to(root.resolve()):
        raise RetirementError("清理报告必须放在备份快照目录之外")
    try:
        write_json(report_path, report)
        bucket = valid_bucket(bucket)
        inventory_path = regular(root / "inventory.json")
        if not HEX64.fullmatch(inventory_sha256) or file_sha256(inventory_path) != inventory_sha256:
            raise RetirementError("盘点文件摘要与管理员确认值不一致")
        inventory = read_small_json(inventory_path)
        if (not isinstance(inventory, dict) or inventory.get("schema_version") != 1 or inventory.get("bucket") != bucket
                or inventory.get("prefix") != PREFIX or inventory.get("version") != VERSION or inventory.get("commit") != COMMIT
                or inventory.get("object_count") != 5 or not isinstance(inventory.get("objects"), list)):
            raise RetirementError("盘点文件身份或范围不合法")
        objects = inventory["objects"]
        validate_backup(root, objects)
        if inventory.get("total_size") != sum(item["size"] for item in objects):
            raise RetirementError("盘点文件总量不一致")
        expected = {item["key"]: {field: item[field] for field in ("key", "size", "etag", "last_modified")} for item in objects}
        report["stage"] = "删除前复核全部远端对象"
        if list_objects(store) != expected:
            raise RetirementError("远端列表已变化，禁止删除")
        for key in KEYS:
            if object_metadata(store.head(key), head=True, key=key) != expected[key]:
                raise RetirementError("远端对象已变化，禁止删除")
        for key in KEYS:
            report.update(stage="逐对象删除前复核", failed_key=key)
            write_json(report_path, report)
            if object_metadata(store.head(key), head=True, key=key) != expected[key]:
                raise RetirementError("删除前对象已变化，停止后续删除")
            report["stage"] = "删除单个已确认对象"
            report["uncertain_keys"] = [key]
            write_json(report_path, report)
            store.delete(key)
            report["deleted_keys"].append(key)
            report["uncertain_keys"] = []
            write_json(report_path, report)
        report.update(stage="确认 CI 前缀为空", failed_key="")
        if list_objects(store, allow_empty=True):
            raise RetirementError("删除后 CI 前缀仍有对象，不能报告完成")
        report.update(status="completed", stage="完成", error="")
        write_json(report_path, report)
        return report
    except Exception:
        report.update(status="failed", error="清理未完成，已停止后续删除；底层错误已隐藏")
        write_json(report_path, report)
        raise RetirementError("清理未完成，请检查报告中的阶段、已删键和待确认键") from None


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        raise RetirementError("参数不合法，凭据只能通过受保护环境变量提供")


def main(argv=None):
    parser = ChineseParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    take = commands.add_parser("snapshot", help="下载并验证固定五对象快照")
    take.add_argument("--output", type=Path, default=Path("retirement"))
    clean = commands.add_parser("apply", help="根据管理员确认的快照删除固定五对象")
    clean.add_argument("--snapshot", type=Path, required=True)
    clean.add_argument("--inventory-sha256", required=True)
    clean.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    bucket = valid_bucket(os.environ.get("R2_BUCKET", "mskdsp-update"))
    store = AwsStore(bucket)
    if args.command == "snapshot":
        inventory = snapshot(store, args.output, bucket=bucket)
        print(json.dumps(dict(message="旧 CI 五对象快照已验证", object_count=5, total_size=inventory["total_size"],
                              inventory_sha256=file_sha256(args.output / "inventory.json")), ensure_ascii=False))
    else:
        result = apply(store, args.snapshot, args.inventory_sha256, args.report, bucket=bucket)
        print(json.dumps(dict(message="旧 CI 五对象退役已完成", status=result["status"], deleted_count=len(result["deleted_keys"])), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("旧 CI 退役操作失败；请检查快照或清理报告，敏感底层输出已隐藏", file=sys.stderr)
        raise SystemExit(1) from None

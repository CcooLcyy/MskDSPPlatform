#!/usr/bin/env python3
"""生成单架构程序更新包，校验并发布到 R2，最后提交通道入口。"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script.release import _write_verified_bundle, inspect_archive
from script.update_common import validate_commit, version_order

PROJECT = "mskdsp-platform"
PLATFORM = "linux-amd64"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_MANIFEST = 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("公开发布地址不允许重定向")


def urlopen(request, timeout=30):
    request.add_header("User-Agent", "mskdsp-platform-publisher/1")
    return build_opener(RejectRedirect()).open(request, timeout=timeout)


def public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("公开根地址格式或端口不合法") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path.strip("/")
            or "\\" in value or any(char.isspace() or ord(char) < 32 for char in value)):
        raise ValueError("公开根地址必须是无凭据、路径和参数的 HTTPS 地址")
    return value.rstrip("/")


def valid_prefix(value: str) -> str:
    if not value or any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in (".", "..") for part in value.split("/")):
        raise ValueError("发布前缀必须是安全的相对路径")
    return value


def encoded_url(base: str, key: str) -> str:
    return base + "/" + "/".join(quote(part, safe="") for part in key.split("/"))


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def validate_metadata(data: dict) -> str:
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ("channel", "version", "commit", "base_version", "build_time")):
        raise ValueError("发布身份必须是包含完整字符串字段的 JSON 对象")
    validate_commit(data["channel"], data["version"], data["commit"])
    base = data["base_version"]
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", base):
        raise ValueError("基础版本必须是无前导零的三段版本")
    if data["version"].split("-", 1)[0] != base:
        raise ValueError("发布版本与基础版本不一致")
    try:
        moment = datetime.fromisoformat(data["build_time"].replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("缺少时区")
    except ValueError as error:
        raise ValueError("构建时间必须是包含时区的 ISO8601 时间") from error
    if data["channel"] != "stable":
        stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dt%H%M%S")
        if not data["version"].startswith(f"{base}-{data['channel']}.{stamp}."):
            raise ValueError("发布版本与构建时间不一致")
    tag = data["version"].replace("+", "-")
    if data.get("image_tag", tag) != tag or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError("镜像标签与展示版本不一致")
    image = data.get("image", "")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/mskdsp-platform:" + re.escape(tag), image):
        raise ValueError("镜像名称与发布身份不一致")
    if "platform" in data and data["platform"] != "linux/amd64":
        raise ValueError("当前发布仅支持 linux/amd64")
    if "published_at" in data and data["published_at"] != data["build_time"]:
        raise ValueError("发布时间与构建时间不一致")
    return tag


def read_binary_identity(path: Path) -> dict:
    result = subprocess.run([str(path.resolve()), "--version-json"], check=True, capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    if len(result.stdout) > MAX_MANIFEST:
        raise ValueError("程序身份输出超过大小限制")
    identity = json.loads(result.stdout)
    if not isinstance(identity, dict):
        raise ValueError("程序身份必须为 JSON 对象")
    return identity


def binary_package(destination: Path, source: Path) -> None:
    with destination.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            member = tarfile.TarInfo(PROJECT)
            member.mode, member.mtime, member.size = 0o755, 0, source.stat().st_size
            with source.open("rb") as binary:
                archive.addfile(member, binary)


def same_release(first: dict, second: dict) -> bool:
    return first == second


def write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"拒绝覆盖已有发布文件：{path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def prepare(*, metadata: Path, image_archive: Path, binary: Path, output: Path,
            public_base_url: str, prefix: str = PROJECT, identity_reader=None,
            bundle_output: Path | None = None, bundle_source: Path = Path(".")) -> dict:
    base, prefix = public_url(public_base_url), valid_prefix(prefix)
    data = json.loads(Path(metadata).read_text(encoding="utf-8"))
    tag = validate_metadata(data)
    binary, image_archive, output = Path(binary), Path(image_archive), Path(output)
    if bundle_output is not None:
        bundle_output = Path(bundle_output)
        if bundle_output.exists():
            raise ValueError("交付输出目录已存在，请使用新目录避免混入旧产物")
    if binary.is_symlink() or not binary.is_file() or not 0 < binary.stat().st_size <= MAX_BINARY:
        raise ValueError("缺少程序或程序大小超过 128 MiB")
    if image_archive.is_symlink() or not image_archive.is_file() or image_archive.stat().st_size == 0:
        raise ValueError("缺少完整镜像包")
    identity = (identity_reader or read_binary_identity)(binary)
    if not isinstance(identity, dict) or identity.get("service") != PROJECT or any(identity.get(key) != data[key] for key in ("version", "channel", "commit", "build_time")):
        raise ValueError("程序身份与发布元数据不一致")
    manifest = {"schema_version": 1, "project": PROJECT,
                **{key: data[key] for key in ("channel", "version", "commit", "build_time", "base_version")},
                "published_at": data["build_time"], "platforms": {}}
    with tempfile.TemporaryDirectory(prefix="platform-release-") as temporary:
        staging = Path(temporary)
        image_name, binary_name = f"{PROJECT}-{tag}-{PLATFORM}-image.tar.gz", f"{PROJECT}-{tag}-{PLATFORM}-binary.tar.gz"
        shutil.copyfile(image_archive, staging / image_name)
        # 镜像只校验一次；离线包使用这份已校验的临时副本和摘要。
        image_id = inspect_archive(staging / image_name, data)
        binary_package(staging / binary_name, binary)
        if (staging / binary_name).stat().st_size > MAX_BINARY:
            raise ValueError("程序压缩包超过 128 MiB 更新限制")
        entry = {"image": data["image"], "image_id": image_id}
        checksums = []
        for marker, name in (("", image_name), ("binary_", binary_name)):
            path = staging / name
            sha = digest(path)
            entry.update({marker + "url": encoded_url(base, f"{prefix}/{data['channel']}/{tag}/{PLATFORM}/{name}"),
                          marker + "sha256": sha, marker + "size": path.stat().st_size})
            checksums.append(f"{sha}  {PLATFORM}/{name}")
        manifest["platforms"][PLATFORM] = entry
        existing = output / "release.json"
        if existing.exists() and not same_release(json.loads(existing.read_text(encoding="utf-8")), manifest):
            raise ValueError("拒绝覆盖输出目录中不同版本或内容的发布")
        # 所有身份和归档检查完成后才落盘，失败不产生可发布入口。
        for name in (image_name, binary_name):
            write_immutable(output / "assets" / tag / PLATFORM / name, (staging / name).read_bytes())
        write_immutable(output / "SHA256SUMS", ("\n".join(checksums) + "\n").encode("utf-8"))
        write_immutable(output / "release.json", json_bytes(manifest))
        write_immutable(output / "latest.json", json_bytes(manifest))
        if bundle_output is not None:
            _write_verified_bundle(data, staging / image_name, bundle_output, Path(bundle_source), image_id, entry["sha256"])
    print(f"R2 资产准备完成：通道={data['channel']} 版本={data['version']} 镜像ID={image_id}")
    return manifest


def get_json(url: str, missing_ok: bool = False) -> dict | None:
    try:
        with urlopen(Request(url, headers={"Cache-Control": "no-cache"})) as response:
            raw = response.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise ValueError("公开清单超过大小限制")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("公开清单必须为 JSON 对象")
        return data
    except HTTPError as error:
        error.close()
        if missing_ok and error.code == 404:
            return None
        raise


def verify_asset(url: str, expected_sha: str, expected_size: int) -> None:
    sha, size = hashlib.sha256(), 0
    with urlopen(Request(url, headers={"Cache-Control": "no-cache"})) as response:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > expected_size:
                raise ValueError("公开资产超过预期长度")
            sha.update(chunk)
    if size != expected_size or sha.hexdigest() != expected_sha:
        raise ValueError("公开资产 SHA256 或长度校验失败")
    print(f"公开资产校验通过：{url}")


def aws_environment() -> dict:
    environment = dict(os.environ)
    for name in ("R2_ACCOUNT_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if name in environment:
            value = environment[name].strip()
            if any(char.isspace() or ord(char) < 32 for char in value):
                raise ValueError("R2 凭据含内部空白或控制字符：" + name)
            environment[name] = value
    return environment | {"AWS_DEFAULT_REGION": "auto", "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}


def upload(source: Path, key: str, bucket: str, endpoint: str, content_type: str, cache_control: str) -> None:
    print(f"上传 R2 对象：{key}")
    subprocess.run(["aws", "s3", "cp", str(source), f"s3://{bucket}/{key}", "--endpoint-url", endpoint,
                    "--region", "auto", "--content-type", content_type, "--cache-control", cache_control,
                    "--only-show-errors"], check=True, env=aws_environment())


def list_channel_objects(bucket: str, endpoint: str, channel_root: str) -> set[str]:
    keys, tokens, token = set(), set(), None
    while True:
        command = ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", channel_root,
                   "--endpoint-url", endpoint, "--region", "auto", "--output", "json", "--no-paginate"]
        if token is not None:
            command += ["--continuation-token", token]
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", env=aws_environment())
        page = json.loads(result.stdout)
        if not isinstance(page, dict) or type(page.get("IsTruncated")) is not bool or not isinstance(page.get("Contents", []), list):
            raise ValueError("R2 对象列表格式不合法")
        for item in page.get("Contents", []):
            key = item.get("Key") if isinstance(item, dict) else None
            if not isinstance(key, str) or not key.startswith(channel_root):
                raise ValueError("R2 对象越出当前通道，拒绝清理")
            keys.add(key)
        if not page["IsTruncated"]:
            return keys
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token or token in tokens:
            raise ValueError("R2 对象列表分页异常")
        tokens.add(token)


def cleanup_old_versions(*, bucket: str, endpoint: str, channel_root: str, keep_keys: set[str], manifest: dict, latest_url: str) -> int:
    if not channel_root.endswith("/") or not keep_keys or any(not key.startswith(channel_root) for key in keep_keys):
        raise ValueError("清理保留集越出当前通道")
    valid_prefix(channel_root.rstrip("/"))
    keys = list_channel_objects(bucket, endpoint, channel_root)
    if not keep_keys <= keys:
        raise ValueError("最新版本对象不完整，拒绝清理")
    if get_json(latest_url) != manifest:
        raise ValueError("公开入口已变化，拒绝清理")
    obsolete = sorted(keys - keep_keys)
    for key in obsolete:
        print(f"删除 R2 旧对象：{key}")
        subprocess.run(["aws", "s3api", "delete-object", "--bucket", bucket, "--key", key,
                        "--endpoint-url", endpoint, "--region", "auto", "--output", "json"],
                       check=True, capture_output=True, env=aws_environment())
    if list_channel_objects(bucket, endpoint, channel_root) != keep_keys:
        raise ValueError("清理后对象列表与保留集不一致，请重跑发布")
    print(f"R2 清理完成：通道={channel_root} 删除={len(obsolete)} 保留={len(keep_keys)}")
    return len(obsolete)


def publish(*, output_dir: Path, bucket: str, public_base_url: str, prefix: str = PROJECT,
            account_id: str | None = None, before_latest: Callable[[], None] | None = None) -> dict:
    environment = aws_environment()
    missing = [name for name in ("R2_ACCOUNT_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if not environment.get(name)]
    if missing:
        raise ValueError("缺少 R2 发布环境变量：" + ", ".join(missing))
    account = environment["R2_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", account) or account_id is not None and account_id != account:
        raise ValueError("R2 账号必须来自环境变量且为 32 位小写十六进制")
    endpoint = f"https://{account}.r2.cloudflarestorage.com"
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("R2 桶名称不合法")
    base, prefix = public_url(public_base_url), valid_prefix(prefix)
    output_dir = Path(output_dir)
    manifest = json.loads((output_dir / "release.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("project") != PROJECT:
        raise ValueError("发布清单项目或 schema_version 不正确")
    if not isinstance(manifest.get("platforms"), dict) or set(manifest["platforms"]) != {PLATFORM}:
        raise ValueError("发布清单必须只包含 linux-amd64")
    entry = manifest["platforms"][PLATFORM]
    if not isinstance(entry, dict):
        raise ValueError("架构资产必须是 JSON 对象")
    tag = validate_metadata(manifest | {"image": entry["image"]})
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.get("image_id", "")):
        raise ValueError("镜像内容 ID 不正确")
    version_key = f"{prefix}/{manifest['channel']}/{tag}"
    assets, checksums = [], []
    for marker, suffix in (("", "image"), ("binary_", "binary")):
        name = f"{PROJECT}-{tag}-{PLATFORM}-{suffix}.tar.gz"
        key = f"{version_key}/{PLATFORM}/{name}"
        path = output_dir / "assets" / tag / PLATFORM / name
        sha, size = entry[marker + "sha256"], entry[marker + "size"]
        if (entry[marker + "url"] != encoded_url(base, key) or not HEX64.fullmatch(sha)
                or type(size) is not int or size <= 0 or not path.is_file()
                or path.stat().st_size != size or digest(path) != sha):
            raise ValueError("本地资产或公开 URL 校验失败：" + name)
        assets.append((path, key, entry[marker + "url"], sha, size, "application/gzip"))
        checksums.append(f"{sha}  {PLATFORM}/{name}")
    sums = output_dir / "SHA256SUMS"
    if sums.read_text(encoding="utf-8") != "\n".join(checksums) + "\n":
        raise ValueError("SHA256SUMS 与发布清单不一致")
    assets.append((sums, version_key + "/SHA256SUMS", encoded_url(base, version_key + "/SHA256SUMS"), digest(sums), sums.stat().st_size, "text/plain; charset=utf-8"))
    immutable_url = encoded_url(base, version_key + "/release.json")
    existing = get_json(immutable_url, missing_ok=True)
    if existing is not None and not same_release(existing, manifest):
        raise ValueError("拒绝覆盖相同版本下不同提交或发布内容")
    latest_key = f"{prefix}/{manifest['channel']}/latest.json"
    latest_url = encoded_url(base, latest_key)
    current = get_json(latest_url, missing_ok=True)
    if current is not None:
        if current.get("schema_version") != 1 or current.get("project") != PROJECT or current.get("channel") != manifest["channel"]:
            raise ValueError("现有入口不属于当前项目与通道")
        new_order, old_order = version_order(manifest["channel"], manifest["version"]), version_order(current["channel"], current["version"])
        if new_order < old_order or new_order == old_order and not same_release(current, manifest):
            raise ValueError("拒绝入口倒退或同版本内容覆盖")
    cache = "public,max-age=31536000,immutable"
    if existing is None:
        for path, key, _, _, _, content_type in assets:
            upload(path, key, bucket, endpoint, content_type, cache)
    for _, _, url, sha, size, _ in assets:
        verify_asset(url, sha, size)
    if existing is None:
        release_file = output_dir / "release.json"
        upload(release_file, version_key + "/release.json", bucket, endpoint, "application/json; charset=utf-8", cache)
        verify_asset(immutable_url, digest(release_file), release_file.stat().st_size)
    # 入口是发布事务的最后提交点，公开读回成功之后才能清理旧版本。
    with tempfile.TemporaryDirectory(prefix="platform-latest-") as temporary:
        latest = Path(temporary) / "latest.json"
        latest.write_bytes(json_bytes(manifest))
        if before_latest is not None:
            before_latest()
        upload(latest, latest_key, bucket, endpoint, "application/json; charset=utf-8", "no-store")
        if get_json(latest_url) != manifest:
            raise ValueError("公开 latest.json 与发布清单不一致")
    cleanup_old_versions(bucket=bucket, endpoint=endpoint, channel_root=f"{prefix}/{manifest['channel']}/",
                         keep_keys={latest_key, version_key + "/release.json", *(key for _, key, *_ in assets)},
                         manifest=manifest, latest_url=latest_url)
    print(f"R2 发布完成：{latest_url}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare", help="准备镜像与单程序更新归档")
    for name in ("metadata", "image-archive", "binary", "output"):
        prepare_command.add_argument("--" + name, type=Path, required=True)
    prepare_command.add_argument("--bundle-output", type=Path, help="同时生成离线包，共用镜像校验和摘要")
    prepare_command.add_argument("--bundle-source", type=Path, default=Path("."), help="离线包部署文件来源")
    publish_command = commands.add_parser("publish", help="发布资产、提交入口并清理当前通道")
    publish_command.add_argument("--output-dir", type=Path, required=True)
    publish_command.add_argument("--bucket", required=True)
    publish_command.add_argument("--account-id", help="可选校验值，必须与 R2_ACCOUNT_ID 环境变量一致")
    for command in (prepare_command, publish_command):
        command.add_argument("--public-base-url", required=True)
        command.add_argument("--prefix", default=PROJECT)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        (prepare if command == "prepare" else publish)(**args)
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError, subprocess.SubprocessError) as error:
        # 不输出子进程命令、环境或 stderr，防止 AWS SDK 诊断泄露凭据。
        message = "发布命令执行失败" if isinstance(error, subprocess.SubprocessError) else str(error)
        parser.exit(1, f"R2 发布失败：{message}\n")


if __name__ == "__main__":
    main()

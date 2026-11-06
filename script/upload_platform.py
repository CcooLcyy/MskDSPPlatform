#!/usr/bin/env python3
"""将冻结的 main CI 程序包上传到平台软件仓库；上传成功不发布或安装。"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import platform_grpc_client as client
from script.release import BEIJING, sha256_file
from script.update_common import validate_commit
from datetime import datetime, timezone


class UploadError(ValueError):
    """只包含可安全输出的诊断，不传播 RPC 详细信息。"""


def package_metadata(data: dict, archive: Path) -> dict:
    if data.get("channel") != "ci" or data.get("ref") != "main":
        raise UploadError("平台上传仅接受 main CI 冻结身份")
    validate_commit("ci", data["version"], data["commit"])
    moment = datetime.fromisoformat(data["build_time"].replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise UploadError("构建时间必须包含时区")
    stamps = [moment.astimezone(zone).strftime("%Y%m%dt%H%M%S") for zone in (BEIJING, timezone.utc)]
    if not any(f"-ci.{stamp}." in data["version"] for stamp in stamps):
        raise UploadError("构建时间与版本不一致")
    source = urlsplit(data["source"])
    if source.scheme not in ("http", "https") or not source.hostname or source.username or source.password or source.query or source.fragment:
        raise UploadError("源码仓库地址必须为无凭据 HTTP(S) 地址")
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= 128 * 1024 * 1024:
        raise UploadError("缺少完整程序包，或大小超过 128 MiB")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", archive.name):
        raise UploadError("程序包文件名不合法")
    return dict(product="mskdsp-platform", version=data["version"], platform="linux-amd64",
                file_name=archive.name, size=archive.stat().st_size, sha256=sha256_file(archive),
                source_repository=data["source"], source_ref="main", source_sha=data["commit"],
                build_time=data["build_time"], channel="ci")


def validate_endpoint(endpoint: str, *, plaintext: bool) -> str:
    try:
        parsed = urlsplit("//" + endpoint)
        port = parsed.port
    except ValueError:
        raise UploadError("gRPC 地址必须为 host:port") from None
    if (not parsed.hostname or not port or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or "\\" in endpoint
            or any(char.isspace() or ord(char) < 32 for char in endpoint)):
        raise UploadError("gRPC 地址必须为无凭据的 host:port")
    if plaintext:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            raise UploadError("明文上传只允许显式内网或回环 IP 地址") from None
        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
        if not any(address in ipaddress.ip_network(network) for network in networks):
            raise UploadError("明文上传只允许显式内网或回环 IP 地址")
    return endpoint


def transfer(stub, messages, archive, metadata, token, *, attempts=4, retry_delay=1,
             timeout=60, chunk_size=1024 * 1024):
    import grpc
    if not 1 <= attempts <= 10 or not 0 < timeout <= 1800 or not 1 <= chunk_size <= 1024 * 1024:
        raise UploadError("上传重试、超时或分块配置不合法")
    if not token or not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise UploadError("PLATFORM_UPLOAD_TOKEN 未配置或格式不合法")
    authorization = (("authorization", "Bearer " + token),)
    expected = messages.PackageMetadata(**metadata)
    retryable = (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.CANCELLED)

    def call(method, request):
        for attempt in range(attempts):
            try:
                return method(request, metadata=authorization, timeout=timeout)
            except grpc.RpcError as error:
                if error.code() not in retryable or attempt == attempts - 1:
                    raise UploadError(f"平台上传 RPC 失败：{error.code().name}") from None
                time.sleep(retry_delay)

    created = call(stub.CreateUpload, messages.CreateUploadRequest(metadata=expected)).upload
    if not created.id:
        raise UploadError("平台未返回上传任务身份")
    upload_id = created.id

    def state():
        current = call(stub.GetUpload, messages.GetUploadRequest(upload_id=upload_id)).upload
        if current.id != upload_id or current.metadata != expected or current.received_bytes > metadata["size"]:
            raise UploadError("平台上传状态与冻结程序包身份不一致")
        if current.state not in ("uploading", "completed"):
            raise UploadError("平台上传任务已失败或过期")
        if current.state == "completed" and (not current.package_id or current.received_bytes != metadata["size"]):
            raise UploadError("平台完成状态缺少包身份或完整长度")
        return current

    def chunks(offset):
        with Path(archive).open("rb") as source:
            source.seek(offset)
            while content := source.read(chunk_size):
                yield messages.UploadPackageRequest(upload_id=upload_id, offset=offset, data=content)
                offset += len(content)

    # 每次重试先查落盘偏移；完整响应丢失也不会重复传数据。
    for attempt in range(attempts):
        current = state()
        if current.state == "completed":
            return {"state": "completed", "package_id": current.package_id, "upload_id": upload_id}
        try:
            if current.received_bytes < metadata["size"]:
                stub.UploadPackage(chunks(current.received_bytes), metadata=authorization, timeout=timeout)
                current = state()
                if current.state == "completed":
                    return {"state": "completed", "package_id": current.package_id, "upload_id": upload_id}
                if current.received_bytes != metadata["size"]:
                    raise UploadError("平台流式上传返回后长度仍不完整")
            package = stub.CompleteUpload(messages.CompleteUploadRequest(upload_id=upload_id),
                                          metadata=authorization, timeout=timeout).package
            if not package.id or package.metadata != expected:
                raise UploadError("平台入库响应与冻结程序包身份不一致")
            current = state()
            if current.state != "completed" or current.package_id != package.id:
                raise UploadError("平台尚未确认程序包完成入库")
            return {"state": "completed", "package_id": current.package_id, "upload_id": upload_id}
        except grpc.RpcError as error:
            if error.code() not in retryable or attempt == attempts - 1:
                # 最后一次 Complete 的响应也可能丢失，最终查询可以确认已完成。
                current = state()
                if current.state == "completed":
                    return {"state": "completed", "package_id": current.package_id, "upload_id": upload_id}
                raise UploadError(f"平台上传 RPC 失败：{error.code().name}") from None
            time.sleep(retry_delay)
    raise UploadError("平台上传重试次数已耗尽")


def upload(*, metadata, archive, proto_root=client.ROOT / "platform-proto", timeout=60):
    data = json.loads(Path(metadata).read_text(encoding="utf-8"))
    identity = package_metadata(data, Path(archive))
    plaintext = os.environ.get("PLATFORM_GRPC_PLAINTEXT", "0") == "1"
    endpoint = validate_endpoint(os.environ.get("PLATFORM_GRPC_ENDPOINT", ""), plaintext=plaintext)
    ca_file = os.environ.get("PLATFORM_GRPC_CA_FILE")
    if plaintext and ca_file:
        raise UploadError("明文上传不能配置 TLS CA 文件")
    token = os.environ.get("PLATFORM_UPLOAD_TOKEN", "")
    if not token:
        raise UploadError("未配置 PLATFORM_UPLOAD_TOKEN")
    client.require_grpc()
    import grpc
    with tempfile.TemporaryDirectory(prefix="platform-upload-") as temporary:
        generated = client.generate_bindings(proto_root, temporary)
        with client.load_bindings(generated) as (messages, services):
            if not all(field in messages.PackageMetadata.DESCRIPTOR.fields_by_name for field in ("build_time", "channel")):
                raise UploadError("固定协议缺少平台 CI 元数据字段，请初始化最新固定子模块")
            if plaintext:
                channel = grpc.insecure_channel(endpoint)
            else:
                roots = Path(ca_file).read_bytes() if ca_file else None
                channel = grpc.secure_channel(endpoint, grpc.ssl_channel_credentials(roots))
            with channel:
                return transfer(services.SoftwarePublishServiceStub(channel), messages, Path(archive), identity,
                                token, timeout=timeout)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit("平台上传参数不合法；令牌仅从 PLATFORM_UPLOAD_TOKEN 环境变量读取")


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=Path("package/metadata.json"))
    parser.add_argument("--archive", type=Path, default=Path("package/binary.tar.gz"))
    parser.add_argument("--proto-root", type=Path, default=client.ROOT / "platform-proto")
    parser.add_argument("--timeout", type=float, default=60)
    result = upload(**vars(parser.parse_args(argv)))
    print(f"平台程序包已完成入库：state={result['state']} package_id={result['package_id']}")


def cli(argv=None):
    try:
        main(argv)
    except Exception:
        # RPC 详情、外部命令异常及环境值均可能包含令牌，CLI 只输出固定诊断。
        raise SystemExit("平台程序包上传失败，请检查冻结身份、权限、地址及网络；底层输出已隐藏以保护凭据") from None


if __name__ == "__main__":
    cli()

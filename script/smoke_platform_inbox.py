#!/usr/bin/env python3
"""在隔离目录验收真实 gRPC 入库、管理员手动升级、持久化重启及失败回滚。"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_grpc_client as grpc_client
import runtime
import smoke_runtime as smoke
import update_common as common


def free_port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


def wait_ready(process, address):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("烟测接包平台进程提前退出")
        try:
            with urlopen(address + "/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, HTTPError):
            pass
        time.sleep(0.1)
    raise RuntimeError("等待接包平台就绪超时")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class SmokeManager(runtime.Manager):
    def healthy(self, state=None, timeout=30):
        return super().healthy(state, timeout=min(timeout, 8))


def login(control, username, password):
    smoke.request_json(control + "/api/v1/auth/login", "POST",
                       {"username": username, "password": password}, "auth")


def update_via_admin(root, config, updater, username, password, identity, *, fail_candidate=False):
    seed = root / "old-platform"
    old = smoke.old_seed(seed, username, password)

    class CandidateManager(SmokeManager):
        def healthy(self, state=None, timeout=30):
            if fail_candidate and state and state.get("version") == identity["version"]:
                return False
            return super().healthy(state, timeout)

    child_http = "127.0.0.1:" + str(free_port())
    manager = CandidateManager(root / "runtime", updater, config=config, seed=seed, child_http=child_http)
    with ExitStack() as stack:
        stack.callback(manager.close)
        manager.start()
        control = smoke.serve(runtime.RuntimeServer(("127.0.0.1", 0), manager), stack)
        # 新实例没有管理员会话，确认匿名安装请求被拒绝。
        smoke.COOKIES.clear()
        smoke.require_login(control)
        try:
            smoke.request_json(control + "/api/update/apply", "POST")
        except HTTPError as failure:
            smoke.require(failure.code == 401, "匿名升级请求必须返回 401")
            failure.close()
        else:
            raise RuntimeError("匿名请求不应允许安装更新")
        login(control, username, password)
        before = manager.current.copy()
        smoke.request_json(control + "/api/update/check", "POST")
        smoke.wait_job(manager, "available", time.monotonic() + 20)
        smoke.require(manager.current == before, "检查更新不能替换运行版本")
        time.sleep(0.2)
        smoke.require(manager.current["version"] == old["version"], "候选入库不能自动安装")
        smoke.request_json(control + "/api/update/apply", "POST")
        smoke.wait_job(manager, "error" if fail_candidate else "succeeded", time.monotonic() + 35)
        if fail_candidate:
            smoke.require(manager.current["version"] == old["version"], "失败后没有恢复旧版")
            smoke.require("回滚" in manager.status()["message"], "失败回滚缺少明确结果")
        else:
            smoke.require(manager.current["version"] == identity["version"], "管理员安装未切换到候选版本")
            smoke.require(manager.current["commit"] == identity["commit"], "已安装提交身份错误")
            smoke.require(manager.healthy(), "新平台 HTTP/gRPC 校验失败")
    if not fail_candidate:
        # 不依赖原接包进程或原始更新 URL，重建管理器仍使用已提交程序。
        restored = SmokeManager(root / "runtime", updater, config=config, seed=seed,
                                child_http="127.0.0.1:" + str(free_port()))
        try:
            restored.start()
            smoke.require(restored.current["commit"] == identity["commit"], "重启后运行身份丢失")
            smoke.require(restored.healthy(), "持久化重启未通过健康检查")
        finally:
            restored.close()


def run(binary, proto_root):
    import grpc
    identity = runtime.binary_identity(binary)
    common.validate_commit(identity["channel"], identity["version"], identity["commit"])
    smoke.require(identity["channel"] == "ci", "烟测需要真实 CI 构建身份")
    with tempfile.TemporaryDirectory(prefix="platform-inbox-smoke-") as directory:
        root = Path(directory)
        data = root / "runtime/data"
        data.mkdir(parents=True)
        sentinel = data / "preserved.txt"
        sentinel.write_text("验证业务目录保持不变", encoding="utf-8")
        username, password, token = "inbox-smoke-admin", secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        hashed = subprocess.run([str(binary), "--hash-admin-password"], input=password, text=True,
                                capture_output=True, check=True, timeout=10).stdout.strip()
        grpc_port, http_port = free_port(), free_port()
        config, updater = root / "config.toml", root / "updater.json"
        config.write_text(f'[server]\nhttp_addr = "127.0.0.1:{http_port}"\ngrpc_addr = "127.0.0.1:{grpc_port}"\n'
                          + '[auth]\nusername = ' + json.dumps(username) + '\npassword_hash = ' + json.dumps(hashed)
                          + '\nsecure_cookie = false\n[software]\nci_upload_token_sha256 = '
                          + json.dumps(hashlib.sha256(token.encode()).hexdigest()) + '\n', encoding="utf-8")
        updater.write_text(json.dumps({"source": "platform", "channel": "ci"}), encoding="utf-8")
        config_before = config.read_bytes()
        archive = root / "platform-binary.tar.gz"
        smoke.binary_archive(archive, binary)
        package_sha = common_hash(archive)
        receiver = subprocess.Popen([str(binary)], env=dict(os.environ,
                                    MSKDSP_PLATFORM_CONFIG=str(config), MSKDSP_PLATFORM_DATA_DIR=str(data)),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_ready(receiver, f"http://127.0.0.1:{http_port}")
            generated = grpc_client.generate_bindings(proto_root, root / "bindings")
            with grpc_client.load_bindings(generated) as (messages, services), \
                    grpc.insecure_channel(f"127.0.0.1:{grpc_port}") as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
                stub = services.SoftwarePublishServiceStub(channel)
                auth = [("authorization", "Bearer " + token)]
                metadata = messages.PackageMetadata(product="mskdsp-platform", platform="linux-amd64",
                            channel="ci", version=identity["version"], build_time=identity["build_time"],
                            source_ref="main", source_sha=identity["commit"], source_repository="https://example.invalid/platform",
                            file_name=archive.name, size=archive.stat().st_size, sha256=package_sha)
                upload = stub.CreateUpload(messages.CreateUploadRequest(metadata=metadata), metadata=auth, timeout=5).upload

                def chunks():
                    offset = 0
                    with archive.open("rb") as source:
                        while part := source.read(1024 * 1024):
                            yield messages.UploadPackageRequest(upload_id=upload.id, offset=offset, data=part)
                            offset += len(part)

                stub.UploadPackage(chunks(), metadata=auth, timeout=30)
                package = stub.CompleteUpload(messages.CompleteUploadRequest(upload_id=upload.id),
                                              metadata=auth, timeout=30).package
                completed = stub.GetUpload(messages.GetUploadRequest(upload_id=upload.id), metadata=auth, timeout=5).upload
                smoke.require(completed.state == "completed" and completed.package_id == package.id, "入库状态不完整")
                try:
                    stub.PublishRelease(messages.PublishReleaseRequest(package_id=package.id, channel="ci"),
                                        metadata=auth, timeout=5)
                except grpc.RpcError as error:
                    smoke.require(error.code() == grpc.StatusCode.PERMISSION_DENIED, "上传令牌没有隔离发布权限")
                else:
                    raise RuntimeError("上传令牌不应拥有发布权限")
        finally:
            stop_process(receiver)
        stored = data / "software/packages/mskdsp-platform" / identity["version"] / "linux-amd64" / archive.name
        smoke.require(common_hash(stored) == package_sha, "落盘包摘要与上传文件不符")
        # 管理器使用和平台一致的显式数据根；禁止任何远端更新下载。
        with patch.dict(os.environ, {"MSKDSP_PLATFORM_DATA_DIR": str(data)}), \
                patch.object(common.Transport, "open", side_effect=AssertionError("本地更新不应发起远端下载")):
            update_via_admin(root, config, updater, username, password, identity)
        rollback = root / "rollback"
        shutil.copytree(data / "software", rollback / "runtime/data/software")
        with patch.dict(os.environ, {"MSKDSP_PLATFORM_DATA_DIR": str(rollback / "runtime/data")}), \
                patch.object(common.Transport, "open", side_effect=AssertionError("回滚烟测不应访问远端更新源")):
            update_via_admin(rollback, config, updater, username, password, identity, fail_candidate=True)
        smoke.require(config.read_bytes() == config_before, "烟测改变了管理员或平台配置")
        smoke.require(sentinel.read_text(encoding="utf-8") == "验证业务目录保持不变", "业务目录未保留")
        print(json.dumps({"result": "通过", "grpc_upload": "真实包入库并校验",
              "admin_update": "检查不安装、管理员升级、健康检查、持久化重启通过",
              "rollback": "隔离失败回滚通过", "remote_update_requests": 0,
              "version": identity["version"], "commit": identity["commit"]}, ensure_ascii=False))


def common_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--proto-root", type=Path, default=Path(__file__).resolve().parents[1] / "platform-proto")
    options = parser.parse_args()
    run(options.binary.resolve(), options.proto_root.resolve())

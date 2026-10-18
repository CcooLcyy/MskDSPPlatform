"""平台 CI 上传：本地假服务验证四个 RPC、续传与令牌边界。"""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import upload_platform as upload
from script import platform_grpc_client as client
from script.release import build_metadata

HAS_GRPC = bool(importlib.util.find_spec("grpc") and importlib.util.find_spec("grpc_tools"))


class UploadTests(unittest.TestCase):
    def test_frozen_identity_and_archive_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "binary.tar.gz"
            binary.write_bytes(b"example archive")
            data = build_metadata("ci", "0.0.1", "0123456789abcdef0123456789abcdef01234567",
                                  "2026-10-18T09:42:36Z", "6.98", "owner/repo")
            data.update(ref="main", source="https://gitlab.test/platform")
            metadata = upload.package_metadata(data, binary)
            self.assertEqual(metadata["product"], "mskdsp-platform")
            self.assertEqual(metadata["platform"], "linux-amd64")
            self.assertEqual(metadata["source_ref"], "main")
            self.assertEqual(metadata["build_time"], data["build_time"])
            self.assertEqual(metadata["size"], binary.stat().st_size)
            self.assertEqual(metadata["sha256"], upload.sha256_file(binary))
            for changes in ({"ref": "feature"}, {"channel": "beta"}, {"commit": "F" * 40},
                            {"build_time": "2026-10-18T09:42:36"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    upload.package_metadata(data | changes, binary)

    def test_plaintext_is_explicit_and_restricted_to_private_addresses(self):
        for endpoint in ("example.com:50000", "8.8.8.8:50000", "http://172.17.0.1:50000", "user:secret@127.0.0.1:1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                upload.validate_endpoint(endpoint, plaintext=True)
        self.assertEqual(upload.validate_endpoint("172.17.0.1:50000", plaintext=True), "172.17.0.1:50000")
        self.assertEqual(upload.validate_endpoint("platform.test:443", plaintext=False), "platform.test:443")

    def test_cli_has_no_token_argument_and_hides_credentials(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"PLATFORM_UPLOAD_TOKEN": "TOP-SECRET"}, clear=True), \
                patch.object(upload, "main", side_effect=RuntimeError("TOP-SECRET")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                upload.cli([])
        self.assertNotIn("TOP-SECRET", str(error.exception) + output.getvalue())

    @unittest.skipUnless(HAS_GRPC, "需要 grpcio 和 grpcio-tools")
    def test_tls_is_default_and_token_is_never_a_cli_option(self):
        data = build_metadata("ci", "0.0.1", "0123456789abcdef0123456789abcdef01234567",
                              "2026-10-18T09:42:36Z", "6.98", "owner/repo")
        data["ref"] = "main"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata, archive = root / "metadata.json", root / "binary.tar.gz"
            metadata.write_text(json.dumps(data), encoding="utf-8")
            archive.write_bytes(b"package")
            messages, services = Mock(), Mock()
            messages.PackageMetadata.DESCRIPTOR.fields_by_name = {"build_time": None, "channel": None}
            @contextlib.contextmanager
            def bindings(directory):
                yield messages, services
            with patch.dict(os.environ, {"PLATFORM_UPLOAD_TOKEN": "local-token", "PLATFORM_GRPC_ENDPOINT": "platform.test:443"}, clear=True), \
                    patch.object(client, "generate_bindings"), patch.object(client, "load_bindings", bindings), \
                    patch("grpc.secure_channel") as secure, patch("grpc.insecure_channel") as insecure, \
                    patch.object(upload, "transfer", return_value={"state": "completed"}):
                upload.upload(metadata=metadata, archive=archive)
            secure.assert_called_once()
            insecure.assert_not_called()
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
            upload.main(["--token", "TOP-SECRET"])
        self.assertNotIn("TOP-SECRET", output.getvalue())

    @unittest.skipUnless(HAS_GRPC, "需要 grpcio 和 grpcio-tools")
    def test_real_grpc_contract_in_isolated_process(self):
        # 旧探测测试使用同名裁剪协议，独立进程避免全局 protobuf descriptor pool 冲突。
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--grpc-contract"],
                                capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def grpc_contract():
    import grpc
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        client.generate_bindings(client.ROOT / "platform-proto", root / "generated")
        with client.load_bindings(root / "generated") as (messages, services):
            fields = messages.PackageMetadata.DESCRIPTOR.fields_by_name
            assert fields["build_time"].number == 14
            assert fields["channel"].number == 15
            class Publisher(services.SoftwarePublishServiceServicer):
                def __init__(self):
                    self.metadata = None
                    self.content = bytearray()
                    self.completed = False
                    self.interrupt = True
                    self.complete_reply_lost = True
                    self.offsets = []
                    self.calls = []
                    self.rejected = False
                def auth(self, context, method):
                    assert dict(context.invocation_metadata())["authorization"] == "Bearer local-test-token"
                    self.calls.append(method)
                def state(self):
                    return messages.Upload(id="upload-1", metadata=self.metadata, received_bytes=len(self.content),
                                           state="completed" if self.completed else "uploading",
                                           package_id="package-1" if self.completed else "")
                def CreateUpload(self, request, context):
                    self.auth(context, "create")
                    if self.metadata is not None and self.metadata != request.metadata:
                        self.rejected = True
                        context.abort(grpc.StatusCode.FAILED_PRECONDITION, "same version different content")
                    self.metadata = request.metadata
                    return messages.CreateUploadResponse(upload=self.state())
                def GetUpload(self, request, context):
                    self.auth(context, "get")
                    assert request.upload_id == "upload-1"
                    return messages.GetUploadResponse(upload=self.state())
                def UploadPackage(self, requests, context):
                    self.auth(context, "stream")
                    for request in requests:
                        assert request.upload_id == "upload-1" and request.offset == len(self.content)
                        self.offsets.append(request.offset)
                        self.content.extend(request.data)
                        if self.interrupt:
                            self.interrupt = False
                            context.abort(grpc.StatusCode.UNAVAILABLE, "response lost after persisted chunk")
                    return messages.UploadPackageResponse(upload=self.state())
                def CompleteUpload(self, request, context):
                    self.auth(context, "complete")
                    assert len(self.content) == self.metadata.size
                    if upload.hashlib.sha256(self.content).hexdigest() != self.metadata.sha256:
                        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "digest mismatch")
                    self.completed = True
                    if self.complete_reply_lost:
                        self.complete_reply_lost = False
                        context.abort(grpc.StatusCode.UNAVAILABLE, "completion response lost")
                    return messages.CompleteUploadResponse(package=messages.Package(id="package-1", metadata=self.metadata))
                def PublishRelease(self, request, context):
                    raise AssertionError("CI must never publish")
            service = Publisher()
            server = grpc.server(ThreadPoolExecutor(max_workers=2))
            services.add_SoftwarePublishServiceServicer_to_server(service, server)
            port = server.add_insecure_port("127.0.0.1:0")
            server.start()
            binary = root / "binary.tar.gz"
            binary.write_bytes(b"1234567890abcdef" * 1000)
            metadata = dict(product="mskdsp-platform", version="0.0.1-ci.test", platform="linux-amd64",
                            file_name=binary.name, size=binary.stat().st_size, sha256=upload.sha256_file(binary),
                            source_ref="main", source_sha="a" * 40, build_time="2026-10-18T09:42:36Z", channel="ci")
            try:
                with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                    stub = services.SoftwarePublishServiceStub(channel)
                    result = upload.transfer(stub, messages, binary, metadata, "local-test-token", chunk_size=1024,
                                             retry_delay=0, attempts=3, timeout=3)
                    assert result == {"state": "completed", "package_id": "package-1", "upload_id": "upload-1"}
                    assert bytes(service.content) == binary.read_bytes()
                    assert service.offsets[:2] == [0, 1024]
                    count = len(service.offsets)
                    assert upload.transfer(stub, messages, binary, metadata, "local-test-token", retry_delay=0)["package_id"] == "package-1"
                    assert len(service.offsets) == count
                    try:
                        upload.transfer(stub, messages, binary, metadata | {"sha256": "0" * 64}, "local-test-token", retry_delay=0)
                    except upload.UploadError:
                        pass
                    else:
                        raise AssertionError("content collision accepted")
                    assert service.rejected
                    service.metadata = None
                    service.content.clear()
                    service.completed = False
                    try:
                        upload.transfer(stub, messages, binary, metadata | {"sha256": "0" * 64}, "local-test-token", retry_delay=0)
                    except upload.UploadError:
                        pass
                    else:
                        raise AssertionError("digest mismatch accepted")
                    assert not service.completed
                    # 不可用服务遵守有限重试，不将 RPC 详情中的令牌泄漏给用户。
                    class AlwaysUnavailable:
                        calls = 0
                        def CreateUpload(self, request, **kwargs):
                            self.calls += 1
                            class Failure(grpc.RpcError):
                                def code(self):
                                    return grpc.StatusCode.UNAVAILABLE
                                def details(self):
                                    return "local-test-token"
                            raise Failure()
                    unavailable = AlwaysUnavailable()
                    try:
                        upload.transfer(unavailable, messages, binary, metadata, "local-test-token", attempts=2, retry_delay=0)
                    except upload.UploadError as error:
                        assert "local-test-token" not in str(error)
                    else:
                        raise AssertionError("unavailable service accepted")
                    assert unavailable.calls == 2
            finally:
                server.stop(0).wait()


if __name__ == "__main__":
    if sys.argv[1:] == ["--grpc-contract"]:
        grpc_contract()
    else:
        unittest.main()

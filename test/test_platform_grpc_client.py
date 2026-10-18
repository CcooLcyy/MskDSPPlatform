"""使用真实生成代码和随机端口验证 CI gRPC 客户端准备及只读探测。"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import platform_grpc_client as client

HAS_GRPC = bool(importlib.util.find_spec("grpc") and importlib.util.find_spec("grpc_tools"))
PROTOCOL = '''syntax = "proto3";
package mskdsp.platform.software.v1;
service SoftwareCatalogService {
  rpc ListPackages(ListPackagesRequest) returns (ListPackagesResponse);
}
message ListPackagesRequest { string product = 1; string channel = 2; string platform = 3; }
message Package { string id = 1; }
message ListPackagesResponse { repeated Package packages = 1; }
'''


class GenerationBoundaryTests(unittest.TestCase):
    # 未初始化或固定提交缺少软件协议时，不生成虚假的客户端文件。
    def test_missing_protocol_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(client.ClientError, "软件仓库协议"):
                client.generate_bindings(root, root / "generated")
            self.assertFalse((root / "generated/software_pb2.py").exists())


@unittest.skipUnless(HAS_GRPC, "需要 grpcio 和 grpcio-tools，未执行真实协议验证")
class GrpcClientTests(unittest.TestCase):
    def setUp(self):
        import grpc
        self.grpc = grpc
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        path = self.root / "platform/v1/software.proto"
        path.parent.mkdir(parents=True)
        path.write_text(PROTOCOL, encoding="utf-8")

    # 真实编译结果可以导入，且不会生成遮蔽标准库的 platform 包。
    def test_generate_imports_client_bindings_without_platform_package(self):
        destination = self.root / "generated"
        client.generate_bindings(self.root, destination)
        self.assertTrue((destination / "software_pb2.py").is_file())
        self.assertTrue((destination / "software_pb2_grpc.py").is_file())
        self.assertFalse((destination / "platform").exists())
        with client.load_bindings(destination) as (messages, services):
            self.assertEqual(messages.ListPackagesRequest(product="mskdsp-platform").product,
                             "mskdsp-platform")
            self.assertTrue(callable(services.SoftwareCatalogServiceStub))

    # 非法协议编译失败必须报告错误，不能返回成功。
    def test_invalid_protocol_is_rejected(self):
        (self.root / "platform/v1/software.proto").write_text("invalid protocol", encoding="utf-8")
        with self.assertRaisesRegex(client.ClientError, "编译"):
            client.generate_bindings(self.root, self.root / "invalid")

    # 探测仅查询目录，不发送发布令牌或执行有副作用的 RPC。
    def test_probe_calls_only_read_only_catalog(self):
        output = self.root / "server-bindings"
        client.generate_bindings(self.root, output)
        received = []
        with client.load_bindings(output) as (messages, services):
            class Catalog(services.SoftwareCatalogServiceServicer):
                def ListPackages(self, request, context):
                    received.append((request.product, request.channel, list(context.invocation_metadata())))
                    return messages.ListPackagesResponse(packages=[messages.Package(id="sample")])

            server = self.grpc.server(ThreadPoolExecutor(max_workers=1))
            services.add_SoftwareCatalogServiceServicer_to_server(Catalog(), server)
            port = server.add_insecure_port("127.0.0.1:0")
            server.start()
            try:
                result = client.probe(self.root, f"127.0.0.1:{port}", plaintext=True, timeout=3)
            finally:
                server.stop(0).wait()
        self.assertEqual(result["package_count"], 1)
        self.assertEqual(result["rpc"], "SoftwareCatalogService.ListPackages")
        self.assertEqual(received[0][:2], ("mskdsp-platform", ""))
        self.assertFalse(any(item.key == "authorization" for item in received[0][2]))

    # 通道成功建立但未部署目录 RPC 时必须明确返回 UNIMPLEMENTED。
    def test_missing_catalog_is_not_reported_as_success(self):
        server = self.grpc.server(ThreadPoolExecutor(max_workers=1))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with self.assertRaisesRegex(client.ClientError, "UNIMPLEMENTED"):
                client.probe(self.root, f"127.0.0.1:{port}", plaintext=True, timeout=3)
        finally:
            server.stop(0).wait()

    # 目标无法建立 gRPC 通道时，按指定截止时间失败。
    def test_unavailable_channel_times_out(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            with self.assertRaisesRegex(client.ClientError, "就绪超时"):
                client.probe(self.root, f"127.0.0.1:{reserved.getsockname()[1]}",
                             plaintext=True, timeout=0.1)

    # 显式明文不能同时接受 CA 配置，避免误以为仍在校验证书。
    def test_plaintext_rejects_ca_configuration(self):
        with self.assertRaisesRegex(client.ClientError, "明文"):
            client.probe(self.root, "127.0.0.1:1", plaintext=True,
                         ca_file=self.root / "missing.pem")


if __name__ == "__main__":
    unittest.main()

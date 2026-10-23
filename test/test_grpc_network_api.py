"""管理进程网络接口的管理员鉴权、严格输入与失败边界。"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import grpc_network
import runtime


def status(**values):
    return dict(enabled=True, mss=1240, revision=1, port=50000, persistent=True,
                applied=True, matched_packets=5, **values)


class AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        authenticated = self.headers.get("Cookie") == "session=valid"
        raw = json.dumps({"username": "root", "role": "admin"} if authenticated else {"error": "登录已失效"}).encode()
        self.send_response(200 if authenticated else 401)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class NetworkAPITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.auth = ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
        self.start(self.auth)
        manager = SimpleNamespace(runtime_dir=Path(self.folder.name), child_http=f"127.0.0.1:{self.auth.server_port}", log=lambda _: None, stopping=threading.Event())
        self.server = runtime.RuntimeServer(("127.0.0.1", 0), manager)
        self.start(self.server)
        self.control = mock.patch.object(grpc_network, "request", return_value=status()).start()
        self.addCleanup(mock.patch.stopall)

    def start(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, method="GET", path="/api/network/status", body=None, headers=None):
        options = {"Cookie": "session=valid", "Content-Type": "application/json", "X-MskDSP-Action": "network"}
        options.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            connection.request(method, path, body, options)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def save(self, value=None, **options):
        value = {"enabled": True, "mss": 1240, "revision": 1} if value is None else value
        return self.request("POST", "/api/network/settings", json.dumps(value), **options)

    def test_unauthorized_and_cross_origin_never_reach_host(self):
        for path in ("/update", "/network", "/api/network/status", "/api/network/settings"):
            self.assertEqual(self.request(path=path, headers={"Cookie": "invalid"})[0], 401)
        for headers in ({"X-MskDSP-Action": "update"}, {"Origin": "https://evil.example"}):
            self.assertEqual(self.save(headers=headers)[0], 403)
        self.control.assert_not_called()

    def test_strict_payload_and_content_type(self):
        for value in ({}, {"enabled": 1, "mss": 1240, "revision": 1},
                      {"enabled": True, "mss": True, "revision": 1},
                      {"enabled": True, "mss": 535, "revision": 1},
                      {"enabled": True, "mss": 1461, "revision": 1},
                      {"enabled": True, "mss": 1240, "revision": False},
                      {"enabled": True, "mss": 1240, "revision": -1},
                      {"enabled": True, "mss": 1240, "revision": 1, "command": "anything"}):
            with self.subTest(value=value):
                self.assertEqual(self.save(value)[0], 400)
        self.assertEqual(self.save(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/network/settings", "x" * 4097)[0], 413)
        self.assertEqual(self.request("POST", "/api/network/settings", '{"enabled":true,"mss":1240,"mss":1300,"revision":1}')[0], 400)
        self.control.assert_not_called()

    def test_status_success_and_revision_conflict(self):
        code, raw = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), status())
        self.assertEqual(self.save()[0], 200)
        self.control.assert_called_with(Path(self.folder.name) / "network-control/control.sock", {"enabled": True, "mss": 1240, "revision": 1})
        self.control.side_effect = grpc_network.ControlError("配置已修改", 409)
        code, raw = self.save()
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(raw), {"error": "配置已修改"})

    def test_unavailable_and_page(self):
        self.control.side_effect = grpc_network.ControlError("服务尚未安装")
        code, raw = self.request()
        self.assertEqual(code, 503)
        self.assertEqual(json.loads(raw), {"error": "服务尚未安装"})
        code, raw = self.request(path="/network")
        self.assertEqual(code, 200)
        self.assertIn("gRPC 网络兼容", raw.decode())

    def test_update_page_has_network_entry_without_rust_page_dependency(self):
        code, raw = self.request(path="/update")
        self.assertEqual(code, 200)
        self.assertIn('href="/network"', raw.decode())
        self.assertIn("平台维护", raw.decode())
        self.assertEqual(self.request("POST", "/update")[0], 405)
        self.assertEqual(self.request(path="/update?anything=1")[0], 400)
        self.control.assert_not_called()

    def test_other_paths_keep_using_existing_http_proxy(self):
        # Fake Rust 后端仅返回身份 JSON，确认请求由原代理读取而非新页面路由接管。
        code, raw = self.request(path="/api/v1/auth/me")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), {"username": "root", "role": "admin"})
        self.control.assert_not_called()

    def test_methods_and_queries(self):
        for method, path, expected in (("POST", "/network", 405), ("POST", "/api/network/status", 405),
                                       ("GET", "/api/network/settings", 405), ("GET", "/api/network/unknown", 404),
                                       ("GET", "/api/network/status?arg=1", 400)):
            self.assertEqual(self.request(method, path)[0], expected)
        self.control.assert_not_called()


class UnixClientTests(unittest.TestCase):
    def test_host_http_response_and_conflicts(self):
        connection = mock.Mock()
        response = connection.getresponse.return_value
        response.status, response.read.return_value = 200, json.dumps(status()).encode()
        with mock.patch.object(grpc_network, "UnixHTTPConnection", return_value=connection):
            self.assertEqual(grpc_network.request("control.sock"), status())
            connection.request.assert_called_with("GET", "/status", None, {"Content-Type": "application/json", "Connection": "close"})
            response.status, response.read.return_value = 409, b'{"error":"conflict"}'
            with self.assertRaises(grpc_network.ControlError) as problem:
                grpc_network.request("control.sock", {"enabled": False, "mss": 1240, "revision": 1})
            self.assertEqual(problem.exception.status, 409)
            response.status, response.read.return_value = 200, b'{}'
            with self.assertRaises(grpc_network.ControlError):
                grpc_network.request("control.sock")
        self.assertEqual(connection.close.call_count, 3)

    def test_write_timeout_is_longer_and_reports_uncertain_outcome(self):
        connection = mock.Mock()
        connection.getresponse.side_effect = TimeoutError("slow response")
        with mock.patch.object(grpc_network, "UnixHTTPConnection", return_value=connection) as constructor:
            with self.assertRaises(grpc_network.ControlError) as problem:
                grpc_network.request("control.sock", {"enabled": True, "mss": 1240, "revision": 1})
            constructor.assert_called_with("control.sock", timeout=30)
            self.assertIn("可能已经生效", str(problem.exception))
            self.assertIn("刷新状态核对", str(problem.exception))
            with self.assertRaises(grpc_network.ControlError):
                grpc_network.request("control.sock")
            constructor.assert_called_with("control.sock", timeout=5)
        self.assertEqual(connection.close.call_count, 2)

    def test_absent_service_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(grpc_network.ControlError) as problem:
                grpc_network.request(Path(directory) / "missing.sock")
            self.assertEqual(problem.exception.status, 503)
            self.assertIn("是否已安装", str(problem.exception))

    def test_invalid_status_never_claims_success(self):
        for value in ({}, {**status(), "persistent": 1}, {**status(), "matched_packets": True}):
            with self.assertRaises(grpc_network.ControlError):
                grpc_network.validate_status(value)

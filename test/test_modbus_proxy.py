"""验证容器管理代理的点表大小限制及版本头透传。"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import runtime


class ProxyTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                result = json.dumps({"length": len(body), "revision": self.headers.get("If-Match"),
                                     "action": self.headers.get("X-MskDSP-Action")}).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)

            def log_message(self, *args):
                pass

        backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        manager = SimpleNamespace(child_http=f"127.0.0.1:{backend.server_port}", stopping=threading.Event())
        self.server = runtime.RuntimeServer(("127.0.0.1", 0), manager)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, path, length):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            limit = (10 if path == "/api/v1/modbus-templates" or path.startswith("/api/v1/modbus-templates/") else 1) * 1024 * 1024
            # 超限在长度头处拒绝，避免 Windows 对尚未读完的大请求触发连接重置。
            connection.request("POST", path, b" " * length if length <= limit else None,
                               {"Content-Length": str(length), "If-Match": "12", "X-MskDSP-Action": "modbus-template"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    # 点表允许通过 10 MiB，版本与操作标记不丢失，超过上限被拒绝。
    def test_template_limits_and_header_forwarding(self):
        base = "/api/v1/modbus-templates"
        for path in (base, base + "/test-id"):
            status, body = self.request(path, 10 * 1024 * 1024)
            self.assertEqual(status, 201)
            self.assertEqual(body, {"length": 10 * 1024 * 1024, "revision": "12", "action": "modbus-template"})
            self.assertEqual(self.request(path, 10 * 1024 * 1024 + 1)[0], 413)

    # 其它业务请求及类似前缀保留 1 MiB 限制。
    def test_other_proxy_limits_unchanged(self):
        for path in ("/echo", "/api/v1/modbus-templates-other"):
            self.assertEqual(self.request(path, 1024 * 1024 + 1)[0], 413)


if __name__ == "__main__":
    unittest.main()

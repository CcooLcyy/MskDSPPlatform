"""以真实平台进程验证点表 HTTP 合约、重启持久化及版本冲突。"""
import copy
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / ("mskdsp-platform.exe" if os.name == "nt" else "mskdsp-platform")


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def template():
    return {
        "schema_version": 1, "id": "test-precision", "name": "逆变器点表", "manufacturer": "测试厂家",
        "models": ["测试型号"], "protocol_version": "V3", "address_base": 1, "notes": ["测试备注"], "sources": [],
        "points": [{"id": "active", "tag": "有功功率", "group": "遥测", "unit": "kW", "function_code": 3,
                    "address": 40122, "data_type": "INT32", "reg_count": 2, "scale": "0.000000000000000123",
                    "offset": "-1e-3", "deadband": "0.01", "word_order": "LH", "byte_order": "BA",
                    "bit_index": None, "source_page": 12, "description": "说明 <script>",
                    "default_selected": False, "condition": "按型号选择"}],
    }


@unittest.skipUnless(BINARY.exists(), "请先执行 cargo build --locked")
class TemplateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.http_port, self.grpc_port = port(), port()
        self.config = self.folder / "config.toml"
        password_hash = subprocess.run([str(BINARY), "--hash-admin-password"], input="test-password-123", text=True,
                                       encoding="utf-8", capture_output=True, check=True).stdout.strip()
        self.config.write_text(f'[server]\nhttp_addr="127.0.0.1:{self.http_port}"\ngrpc_addr="127.0.0.1:{self.grpc_port}"\n'
                               f'[auth]\npassword_hash="{password_hash}"\n', encoding="utf-8")
        self.child = None
        self.cookie = ""
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        env = dict(os.environ, MSKDSP_PLATFORM_CONFIG=str(self.config), MSKDSP_PLATFORM_DATA_DIR=str(self.folder / "data"))
        env.pop("MSKDSP_PLATFORM_HTTP_ADDR", None)
        self.child = subprocess.Popen([str(BINARY)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if self.child.poll() is not None:
                self.fail("平台进程提前退出")
            try:
                if self.request("GET", "/healthz")[0] == 200:
                    status, raw, headers = self.request("POST", "/api/v1/auth/login", {"username": "root", "password": "test-password-123"}, {"X-MskDSP-Action": "auth"})
                    self.assertEqual(status, 200, raw)
                    self.cookie = headers["set-cookie"].split(";", 1)[0]
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("平台 HTTP 未就绪")

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            self.child.wait(timeout=5)

    def request(self, method, path, body=None, headers=None):
        marked = {"Content-Type": "application/json", "X-MskDSP-Action": "modbus-template"}
        if self.cookie:
            marked["Cookie"] = self.cookie
        marked.update(headers or {})
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=5)
        try:
            connection.request(method, path, raw, marked)
            response = connection.getresponse()
            content = response.read()
            return response.status, content, {key.lower(): value for key, value in response.getheaders()}
        finally:
            connection.close()

    # 点表全字段及十进制精度经创建、下载、替换和进程重启保持一致。
    def test_roundtrip_restart_edit_and_revision_conflict(self):
        base = "/api/v1/modbus-templates"
        status, raw, _ = self.request("POST", base, template())
        self.assertEqual(status, 201, raw)
        record = json.loads(raw)
        path = base + "/" + record["id"]
        self.assertEqual(record["template"], template())
        self.assertEqual(json.loads(self.request("GET", path + "/download")[1]), template())
        self.stop()
        self.start()
        self.assertEqual(json.loads(self.request("GET", path)[1]), record)
        listing = json.loads(self.request("GET", base)[1])
        self.assertEqual(listing[0]["point_count"], 1)
        self.assertNotIn("template", listing[0])
        edited = template()
        edited["points"][0]["tag"] = "修改后的点位"
        status, raw, _ = self.request("PUT", path, edited, {"If-Match": str(record["revision"])})
        self.assertEqual(status, 200, raw)
        latest = json.loads(raw)
        self.assertEqual(latest["revision"], record["revision"] + 1)
        self.assertEqual(self.request("PUT", path, template(), {"If-Match": str(record["revision"])})[0], 409)
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": str(record["revision"])})[0], 409)
        self.assertEqual(json.loads(self.request("GET", path)[1])["template"], edited)
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": str(latest["revision"])})[0], 204)
        self.assertEqual(self.request("GET", path)[0], 404)

    # 写操作校验标记、版本、JSON 和上位机不支持的模板，并拒绝超限文件。
    def test_invalid_requests(self):
        base = "/api/v1/modbus-templates"
        for method, path in (("GET", base), ("POST", base), ("PUT", base + "/missing"),
                             ("DELETE", base + "/missing"), ("GET", base + "/missing/download")):
            self.assertEqual(self.request(method, path, template() if method in ("POST", "PUT") else None, {"Cookie": ""})[0], 401)
        self.assertEqual(self.request("POST", base, template(), {"X-MskDSP-Action": ""})[0], 403)
        self.assertEqual(self.request("POST", base, template(), {"Origin": "https://evil.example"})[0], 403)
        for mutate in (lambda x: x.update(schema_version=2), lambda x: x.update(points=[]),
                       lambda x: x["points"][0].update(scale=1.0), lambda x: x["points"][0].update(function_code=2),
                       lambda x: x["points"].append(copy.deepcopy(x["points"][0]))):
            invalid = template()
            mutate(invalid)
            status, raw, _ = self.request("POST", base, invalid)
            self.assertEqual(status, 400, raw)
            self.assertTrue(json.loads(raw)["error"])
        self.assertEqual(self.request("POST", base, {"padding": "x" * (10 * 1024 * 1024)})[0], 413)
        record = json.loads(self.request("POST", base, template())[1])
        self.assertEqual(self.request("PUT", base + "/" + record["id"], template())[0], 428)
        self.assertEqual(self.request("DELETE", base + "/" + record["id"], headers={"If-Match": "abc"})[0], 400)
        self.assertEqual(json.loads(self.request("GET", base)[1])[0]["point_count"], 1)

    # 迁移保留旧记录身份、时间和精度，重启不复活数据库中删除的记录。
    def test_legacy_json_migrates_to_sqlite_once(self):
        self.stop()
        directory = self.folder / "data" / "modbus-templates"
        # 使用全新目录模拟尚未运行数据库版程序的现场。
        legacy_directory = self.folder / "legacy-data"
        directory.rename(legacy_directory)
        directory.mkdir()
        identifier = str(uuid.uuid4())
        record = {"id": identifier, "revision": 7, "created_at": 123, "updated_at": 456, "template": template()}
        filename = identifier + ".json"
        (directory / filename).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.start()
        path = "/api/v1/modbus-templates/" + identifier
        self.assertEqual(json.loads(self.request("GET", path)[1]), record)
        database = directory / "templates.sqlite3"
        self.assertTrue(database.is_file())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertFalse((directory / "legacy-json").exists())
        self.assertFalse((directory / filename).exists())
        self.assertTrue((directory / "database-required.json").is_dir())
        self.assertEqual(list(directory.glob("*.json")), [directory / "database-required.json"])
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": "7"})[0], 204)
        self.stop()
        self.start()
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(json.loads(self.request("GET", "/api/v1/modbus-templates")[1]), [])

    # 新容量允许 10000 点完整往返，10001 点仍返回校验错误。
    def test_expanded_point_capacity_roundtrip(self):
        data = template()
        point = data["points"][0]
        data["points"] = [dict(point, id=f"point-{index}", tag=f"点位{index}") for index in range(10000)]
        status, raw, _ = self.request("POST", "/api/v1/modbus-templates", data)
        self.assertEqual(status, 201, raw[:1000])
        record = json.loads(raw)
        self.assertEqual(record["template"], data)
        download = self.request("GET", "/api/v1/modbus-templates/" + record["id"] + "/download")
        self.assertEqual(download[0], 200)
        self.assertEqual(json.loads(download[1]), data)
        data["points"].append(dict(point, id="overflow", tag="超限点位"))
        self.assertEqual(self.request("POST", "/api/v1/modbus-templates", data)[0], 400)

    # 平台入口和点表页面均可访问，现有程序更新页面继续保留。
    def test_pages_and_builtin_fixture_compatibility(self):
        self.assertIn("点表".encode(), self.request("GET", "/modbus-templates")[1])
        self.assertIn(b"/modbus-templates", self.request("GET", "/")[1])
        self.assertEqual(self.request("GET", "/update")[0], 200)
        fixtures = sorted((ROOT / "test/fixtures/modbus-templates").glob("*.json"))
        self.assertGreaterEqual(len(fixtures), 3, "平台仓库需包含三个厂家点表夹具")
        for file in fixtures:
            expected = json.loads(file.read_text(encoding="utf-8-sig"))
            status, raw, _ = self.request("POST", "/api/v1/modbus-templates", expected)
            self.assertEqual(status, 201, file.name + ": " + raw.decode())
            record = json.loads(raw)
            self.assertEqual(record["template"], expected)


if __name__ == "__main__":
    unittest.main()

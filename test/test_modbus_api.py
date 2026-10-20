"""以真实平台进程验证点表 HTTP 合约、重启持久化及版本冲突。"""
import copy
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / ("mskdsp-platform.exe" if os.name == "nt" else "mskdsp-platform")
STARTUP_TIMEOUT = 30.0


def temporary_api_directory():
    """只隔离真实进程夹具，其他测试继续使用自己的默认临时目录。"""
    configured = os.environ.get("MSKDSP_PLATFORM_TEST_TMPDIR", "")
    if not configured:
        return tempfile.TemporaryDirectory(prefix="mskdsp-platform-api-")
    try:
        root = Path(configured).resolve(strict=True)
        if not root.is_dir():
            raise OSError("不是目录")
        free = shutil.disk_usage(root).free
        # 当前整套夹具实测峰值 3.58 MiB，保留至少八倍余量；不足时不能回退到磁盘。
        if free < 32 * 1024 * 1024:
            raise RuntimeError(f"平台真实进程测试数据目录至少需要 32 MiB 可用空间，当前 {free / 1024 / 1024:.2f} MiB：{root}")
        return tempfile.TemporaryDirectory(prefix="mskdsp-platform-api-", dir=root)
    except OSError as error:
        raise RuntimeError(f"平台真实进程测试数据目录不可用：{configured}（{type(error).__name__}）") from error


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
        self.temp = temporary_api_directory()
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
        temporary = self.folder / "tmp"
        temporary.mkdir(exist_ok=True)
        env = dict(os.environ, MSKDSP_PLATFORM_CONFIG=str(self.config), MSKDSP_PLATFORM_DATA_DIR=str(self.folder / "data"),
                   TMPDIR=str(temporary), RUST_LOG="mskdsp_platform=info,tower_http=warn")
        env.pop("MSKDSP_PLATFORM_HTTP_ADDR", None)
        self.cookie = ""
        self.child = None
        logs = ROOT / "package" / "logs" / "platform-startup"
        logs.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(prefix=self._testMethodName + "-", suffix=".log", dir=logs,
                                         delete=False) as output:
            self.startup_log = Path(output.name)
            try:
                self.child = subprocess.Popen([str(BINARY)], env=env, stdout=output, stderr=subprocess.STDOUT)
            except OSError as error:
                self.startup_failure("平台进程创建失败", started, type(error).__name__)
        deadline = started + STARTUP_TIMEOUT
        last_probe = "尚未探测"
        while True:
            if self.child.poll() is not None:
                self.startup_failure("平台进程提前退出", started, last_probe)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.startup_failure("平台 HTTP 未就绪", started, last_probe)
            try:
                status, _, _ = self.request("GET", "/healthz", timeout=min(0.5, remaining))
                last_probe = f"HTTP {status}"
                if time.monotonic() >= deadline:
                    self.startup_failure("平台 HTTP 未就绪", started, last_probe)
                if status == 200:
                    break
            except (OSError, http.client.HTTPException) as error:
                last_probe = type(error).__name__
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        # 健康探测就绪后只登录一次，保留业务请求原有超时，不掩盖认证错误。
        try:
            status, _, headers = self.request("POST", "/api/v1/auth/login", {"username": "root", "password": "test-password-123"}, {"X-MskDSP-Action": "auth"})
        except (OSError, http.client.HTTPException) as error:
            self.startup_failure("平台测试登录请求失败", started, type(error).__name__)
        if status != 200 or "set-cookie" not in headers:
            self.startup_failure("平台测试登录失败", started, f"HTTP {status}")
        self.cookie = headers["set-cookie"].split(";", 1)[0]

    def startup_failure(self, reason, started, last_probe):
        # 日志留在 CI 产物目录，错误中仅附末尾片段，不输出配置或登录响应。
        try:
            with self.startup_log.open("rb") as output:
                output.seek(0, os.SEEK_END)
                output.seek(max(0, output.tell() - 8192))
                tail = output.read().decode("utf-8", errors="replace")
        except OSError as error:
            tail = f"日志读取失败：{type(error).__name__}"
        code = self.child.poll() if self.child is not None else "未创建"
        self.fail(f"{reason}；已等待 {time.monotonic() - started:.2f} 秒；退出码={code}；"
                  f"HTTP=127.0.0.1:{self.http_port}；gRPC=127.0.0.1:{self.grpc_port}；"
                  f"最后探测={last_probe}；启动日志={self.startup_log}\n日志末尾：\n{tail}")

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)

    def request(self, method, path, body=None, headers=None, *, timeout=5):
        marked = {"Content-Type": "application/json", "X-MskDSP-Action": "modbus-template"}
        if self.cookie:
            marked["Cookie"] = self.cookie
        marked.update(headers or {})
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=timeout)
        try:
            connection.request(method, path, raw, marked)
            response = connection.getresponse()
            content = response.read()
            return response.status, content, {key.lower(): value for key, value in response.getheaders()}
        finally:
            connection.close()

    # 登录前即可加载公共样式；页面 CSP 允许同源样式并保留安全限制。
    def test_shared_styles_are_public_and_allowed_by_page_csp(self):
        status, raw, headers = self.request("GET", "/assets/platform.css", headers={"Cookie": ""})
        self.assertEqual(status, 200, raw)
        self.assertIn("text/css", headers["content-type"])
        self.assertIn(b".app-shell", raw)
        self.assertIn("no-store", headers["cache-control"])
        for path in ("/login", "/update", "/modbus-templates"):
            with self.subTest(path=path):
                status, page, headers = self.request("GET", path, headers={"Cookie": ""} if path == "/login" else None)
                self.assertEqual(status, 200, page)
                self.assertIn(b'href="/assets/platform.css"', page)
                self.assertIn("style-src 'self' 'unsafe-inline'", headers["content-security-policy"])
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])

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

    # 误删进入回收站保留九十天，重启后恢复原 ID、完整内容及创建时间。
    def test_trash_restore_persists_and_rejects_stale_revisions(self):
        base = "/api/v1/modbus-templates"
        record = json.loads(self.request("POST", base, template())[1])
        path = base + "/" + record["id"]
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": str(record["revision"])})[0], 204)
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", path + "/download")[0], 404)
        self.assertEqual(self.request("PUT", path, template(), {"If-Match": str(record["revision"])})[0], 404)
        self.assertEqual(json.loads(self.request("GET", base)[1]), [])
        status, raw, _ = self.request("GET", base + "/trash")
        self.assertEqual(status, 200, raw)
        trash = json.loads(raw)
        self.assertEqual(len(trash), 1)
        deleted = trash[0]
        self.assertEqual(deleted["id"], record["id"])
        self.assertEqual(deleted["revision"], record["revision"] + 1)
        self.assertEqual(deleted["expires_at"] - deleted["deleted_at"], 90 * 24 * 60 * 60 * 1000)
        self.assertEqual(deleted["point_count"], len(template()["points"]))
        self.assertNotIn("template", deleted)
        self.stop()
        self.start()
        self.assertEqual(json.loads(self.request("GET", base + "/trash")[1]), trash)
        restore = base + "/trash/" + record["id"] + "/restore"
        self.assertEqual(self.request("POST", restore)[0], 428)
        self.assertEqual(self.request("POST", restore, headers={"If-Match": str(record["revision"])})[0], 409)
        status, raw, _ = self.request("POST", restore, headers={"If-Match": str(deleted["revision"])})
        self.assertEqual(status, 200, raw)
        restored = json.loads(raw)
        self.assertEqual(restored["id"], record["id"])
        self.assertEqual(restored["template"], record["template"])
        self.assertEqual(restored["created_at"], record["created_at"])
        self.assertGreaterEqual(restored["updated_at"], record["updated_at"])
        self.assertEqual(restored["revision"], deleted["revision"] + 1)
        self.assertEqual(json.loads(self.request("GET", base + "/trash")[1]), [])
        self.assertEqual(json.loads(self.request("GET", path)[1]), restored)
        self.assertEqual(self.request("PUT", path, template(), {"If-Match": str(record["revision"])})[0], 409)
        self.assertEqual(self.request("DELETE", path, headers={"If-Match": str(record["revision"])})[0], 409)
        self.assertEqual(self.request("POST", restore, headers={"If-Match": str(deleted["revision"])})[0], 404)

    # 回收站和恢复继承管理员认证、同源与操作标记保护，失败不丢弃记录。
    def test_trash_requires_admin_action_origin_and_revision(self):
        base = "/api/v1/modbus-templates"
        record = json.loads(self.request("POST", base, template())[1])
        self.assertEqual(self.request("DELETE", base + "/" + record["id"], headers={"If-Match": "1"})[0], 204)
        restore = base + "/trash/" + record["id"] + "/restore"
        self.assertEqual(self.request("GET", base + "/trash", headers={"Cookie": ""})[0], 401)
        self.assertEqual(self.request("POST", restore, headers={"Cookie": "", "If-Match": "2"})[0], 401)
        self.assertEqual(self.request("POST", restore, headers={"X-MskDSP-Action": "", "If-Match": "2"})[0], 403)
        self.assertEqual(self.request("POST", restore, headers={"Origin": "https://evil.example", "If-Match": "2"})[0], 403)
        self.assertEqual(self.request("POST", restore, headers={"If-Match": "invalid"})[0], 400)
        self.assertEqual(len(json.loads(self.request("GET", base + "/trash")[1])), 1)

    # 平台重启清理已过期回收站记录，不清理正常点表，也不能恢复过期条目。
    def test_expired_trash_is_purged_on_restart_without_losing_active_records(self):
        base = "/api/v1/modbus-templates"
        deleted = json.loads(self.request("POST", base, template())[1])
        self.assertEqual(self.request("DELETE", base + "/" + deleted["id"], headers={"If-Match": "1"})[0], 204)
        active = json.loads(self.request("POST", base, template())[1])
        self.stop()
        database = self.folder / "data/modbus-templates/templates.sqlite3"
        expires_at = int(time.time() * 1000) - 1
        deleted_at = expires_at - 90 * 24 * 60 * 60 * 1000
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE trash SET deleted_at=?,expires_at=? WHERE id=?",
                               (f"{deleted_at:020d}", f"{expires_at:020d}", deleted["id"]))
            connection.commit()
        self.start()
        self.assertEqual(json.loads(self.request("GET", base + "/trash")[1]), [])
        self.assertEqual(self.request("POST", base + "/trash/" + deleted["id"] + "/restore", headers={"If-Match": "2"})[0], 404)
        self.assertEqual(json.loads(self.request("GET", base + "/" + active["id"])[1]), active)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM trash").fetchone()[0], 0)

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
        workbench = self.request("GET", "/modbus-templates")[1]
        self.assertIn("点表".encode(), workbench)
        self.assertEqual(self.request("GET", "/")[1], workbench)
        login = self.request("GET", "/login")
        self.assertEqual(login[0], 303)
        self.assertEqual(login[2]["location"], "/modbus-templates")
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

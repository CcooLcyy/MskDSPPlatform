"""真实平台进程验证 Web 账号管理、重启持久化与客户端 HTTP 认证入口移除。"""
from contextlib import closing
import json
import sqlite3
import time
import unittest

import test_modbus_api as platform_fixture


@unittest.skipUnless(platform_fixture.BINARY.exists(), "请先执行 cargo build --locked")
class AccountApiTests(unittest.TestCase):
    setUp = platform_fixture.TemplateApiTests.setUp
    start = platform_fixture.TemplateApiTests.start
    stop = platform_fixture.TemplateApiTests.stop
    request = platform_fixture.TemplateApiTests.request
    startup_failure = platform_fixture.TemplateApiTests.startup_failure

    # 永久删除同时清理持久会话，重启后同名重建使用新身份且不继承旧设备授权。
    def test_delete_account_sessions_restart_and_recreate(self):
        action = {"X-MskDSP-Action": "account"}
        device_action = {"X-MskDSP-Action": "device"}
        credentials = {"username": "delete.user", "password": "delete-password-123"}
        status, raw, _ = self.request("POST", "/api/v1/accounts", credentials, action)
        self.assertEqual(status, 201, raw)
        account = json.loads(raw)
        path = "/api/v1/accounts/" + account["id"]
        status, raw, _ = self.request("POST", "/api/v1/devices", {"display_name": "删除账号回归设备"}, device_action)
        self.assertEqual(status, 201, raw)
        device_path = "/api/v1/devices/" + json.loads(raw)["device_id"]
        grants = [{"account_id": account["id"], "allow_grpc": True, "allow_ssh": True}]
        self.assertEqual(self.request("PUT", device_path + "/grants", grants, device_action)[0], 204)
        database = self.folder / "data/accounts/accounts.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            now = int(time.time() * 1000)
            db.execute(
                "INSERT INTO client_sessions_v1 "
                "(token_hash, account_id, auth_version, issued_at, expires_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ["a" * 64, account["id"], 1, now, now + 60000, now],
            )
            db.commit()
        for headers, expected in ((dict(action, Cookie=""), 401),
                                  ({"X-MskDSP-Action": "wrong"}, 403),
                                  (dict(action, Origin="https://other.example"), 403)):
            self.assertEqual(self.request("DELETE", path, {"revision": 1}, headers)[0], expected)
        self.assertEqual(self.request("DELETE", path, {"revision": 2}, action)[0], 409)
        self.assertEqual(self.request("GET", path)[0], 200)
        status, raw, _ = self.request("DELETE", path, {"revision": 1}, action)
        self.assertEqual(status, 204, raw)
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("DELETE", path, {"revision": 1}, action)[0], 404)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM accounts WHERE id=?", [account["id"]]).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM client_sessions_v1 WHERE account_id=?", [account["id"]]).fetchone()[0], 0)
        self.stop()
        self.start()
        self.assertEqual(self.request("GET", path)[0], 404)
        status, raw, _ = self.request("POST", "/api/v1/accounts", credentials, action)
        self.assertEqual(status, 201, raw)
        replacement = json.loads(raw)
        self.assertNotEqual(replacement["id"], account["id"])
        status, raw, _ = self.request("GET", device_path + "/grants")
        self.assertEqual(status, 200, raw)
        self.assertNotIn(replacement["id"], [grant["account_id"] for grant in json.loads(raw)])

    # 一个更新请求保存设备与授权，未知账号校验失败不能留下设备名称或启用状态的部分修改。
    def test_device_settings_and_permissions_save_together(self):
        action = {"X-MskDSP-Action": "device"}
        status, raw, _ = self.request("POST", "/api/v1/accounts",
                                     {"username": "device.user", "password": "device-password-123"},
                                     {"X-MskDSP-Action": "account"})
        self.assertEqual(status, 201, raw)
        account = json.loads(raw)
        status, raw, _ = self.request("POST", "/api/v1/devices", {"display_name": "原设备"}, action)
        self.assertEqual(status, 201, raw)
        path = "/api/v1/devices/" + json.loads(raw)["device_id"]
        grants = [{"account_id": account["id"], "allow_grpc": True, "allow_ssh": False}]
        status, raw, _ = self.request("PUT", path, {"display_name": "统一保存", "enabled": True, "grants": grants}, action)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["display_name"], "统一保存")
        self.assertEqual(json.loads(self.request("GET", path + "/grants")[1]), grants)
        invalid = [{"account_id": "missing-account", "allow_grpc": True, "allow_ssh": True}]
        status, raw, _ = self.request("PUT", path, {"display_name": "不应写入", "enabled": False, "grants": invalid}, action)
        self.assertEqual(status, 400, raw)
        saved = json.loads(self.request("GET", path)[1])
        self.assertEqual(saved["display_name"], "统一保存")
        self.assertTrue(saved["enabled"])
        self.assertEqual(json.loads(self.request("GET", path + "/grants")[1]), grants)
        self.stop()
        self.start()
        self.assertEqual(json.loads(self.request("GET", path)[1])["display_name"], "统一保存")
        self.assertEqual(json.loads(self.request("GET", path + "/grants")[1]), grants)

    def test_role_assignment_conflict_validation_and_restart(self):
        action = {"X-MskDSP-Action": "account"}
        status, raw, _ = self.request("POST", "/api/v1/accounts",
                                     {"username": "role.user", "password": "field-password-1"}, action)
        self.assertEqual(status, 201, raw)
        account = json.loads(raw)
        self.assertEqual(account["role"], "operator")
        path = "/api/v1/accounts/" + account["id"]
        update = {"revision": 1, "display_name": "现场管理员", "enabled": True, "role": "admin"}
        status, raw, _ = self.request("PUT", path, update, action)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["role"], "admin")
        self.assertEqual(self.request("PUT", path, update, action)[0], 409)
        for role in ("", "root", "ADMIN"):
            self.assertEqual(self.request("PUT", path, dict(update, revision=2, role=role, enabled=False), action)[0], 400)
        # 兼容旧管理客户端：不传角色，原管理员角色仍保留。
        status, raw, _ = self.request("PUT", path, {"revision": 2, "display_name": "现场管理员", "enabled": True}, action)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["role"], "admin")
        database = self.folder / "data/accounts/accounts.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT role,revision,auth_version FROM accounts WHERE id=?", [account["id"]]).fetchone(), ("admin", 3, 1))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.stop()
        self.start()
        status, raw, _ = self.request("GET", path)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["role"], "admin")
        self.assertEqual(json.loads(raw)["revision"], 3)
        status, raw, _ = self.request("GET", "/api/v1/accounts")
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)[0]["role"], "admin")
        status, raw, _ = self.request("POST", "/api/v1/accounts",
                                     {"username": "explicit.admin", "password": "field-password-1", "role": "admin"}, action)
        self.assertEqual(status, 201, raw)
        self.assertEqual(json.loads(raw)["role"], "admin")

    # Web 创建、禁用、重新启用和重置密码均持久化，重启后仍保留账号且不泄露密码。
    def test_account_management_and_restart(self):
        status, page, _ = self.request("GET", "/accounts")
        self.assertEqual(status, 200)
        self.assertIn("账号管理", page.decode())
        action = {"X-MskDSP-Action": "account"}
        status, raw, _ = self.request("POST", "/api/v1/accounts",
                                     {"username": "Site.User", "display_name": "现场维护", "password": "field-password-1"}, action)
        self.assertEqual(status, 201, raw)
        account = json.loads(raw)
        self.assertEqual(account["username"], "site.user")
        path = "/api/v1/accounts/" + account["id"]
        database = self.folder / "data/accounts/accounts.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            initial_hash, = db.execute("SELECT password_hash FROM accounts WHERE id=?", [account["id"]]).fetchone()
            self.assertTrue(initial_hash.startswith("$argon2id$"))
            self.assertNotIn("field-password-1", initial_hash)
        status, raw, _ = self.request("PUT", path, {"revision": 1, "display_name": "现场维护", "enabled": False}, action)
        self.assertEqual(status, 200, raw)
        self.assertFalse(json.loads(raw)["enabled"])
        self.assertEqual(self.request("PUT", path, {"revision": 1, "display_name": "旧版本", "enabled": True}, action)[0], 409)
        status, raw, _ = self.request("PUT", path, {"revision": 2, "display_name": "现场维护", "enabled": True}, action)
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)["enabled"])
        self.assertEqual(self.request("POST", path + "/password", {"revision": 3, "password": "field-password-2"}, action)[0], 200)
        with closing(sqlite3.connect(database)) as db:
            reset_hash, = db.execute("SELECT password_hash FROM accounts WHERE id=?", [account["id"]]).fetchone()
            self.assertTrue(reset_hash.startswith("$argon2id$"))
            self.assertNotEqual(reset_hash, initial_hash)
            self.assertNotIn("field-password-2", reset_hash)
        self.stop()
        self.start()
        status, raw, _ = self.request("GET", path)
        self.assertEqual(status, 200)
        saved = json.loads(raw)
        self.assertEqual(saved["revision"], 4)
        self.assertEqual(saved["username"], "site.user")
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["display_name"], "现场维护")
        self.assertNotIn("password", raw.decode())
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT password_hash FROM accounts WHERE id=?", [account["id"]]).fetchone()[0], reset_hash)
        self.assertNotIn("field-password", self.startup_log.read_text(encoding="utf-8"))

    # HTTP 仅提供 Web 管理；旧客户端认证路径在有无管理员 Cookie 时均为 404，且不触发账号登录。
    def test_client_http_auth_routes_are_removed(self):
        action = {"X-MskDSP-Action": "account"}
        status, raw, _ = self.request("POST", "/api/v1/accounts",
                                     {"username": "site.user", "password": "field-password-1"}, action)
        self.assertEqual(status, 201, raw)
        account = json.loads(raw)
        for method, path in (("POST", "/api/v1/client/auth/login"),
                             ("GET", "/api/v1/client/auth/me"),
                             ("POST", "/api/v1/client/auth/logout")):
            for cookie in ("", self.cookie):
                with self.subTest(method=method, path=path, authenticated=bool(cookie)):
                    self.assertEqual(self.request(method, path,
                                                  {"username": "site.user", "password": "field-password-1"},
                                                  {"X-MskDSP-Action": "client-auth", "Cookie": cookie})[0], 404)
        with closing(sqlite3.connect(self.folder / "data/accounts/accounts.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT last_login_at FROM accounts WHERE id=?", [account["id"]]).fetchone()[0])

    # 普通账号不能通过管理员入口登录，旧实例接口移除，跨源请求不能创建账号。
    def test_manager_boundary_and_simplified_entry(self):
        self.assertEqual(self.request("GET", "/agents")[2]["location"], "/accounts")
        self.assertEqual(self.request("GET", "/api/v1/agents")[0], 404)
        self.assertEqual(self.request("POST", "/api/v1/accounts", {"username": "test.user", "password": "field-password"},
                                      {"X-MskDSP-Action": "account", "Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/v1/accounts", {"username": "root", "password": "field-password"},
                                      {"X-MskDSP-Action": "account"})[0], 400)


if __name__ == "__main__":
    unittest.main()

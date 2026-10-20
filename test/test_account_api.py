"""真实平台进程验证 Web 账号管理、上位机登录与重启持久化。"""
from contextlib import closing
import json
import sqlite3
import unittest

import test_modbus_api as platform_fixture


@unittest.skipUnless(platform_fixture.BINARY.exists(), "请先执行 cargo build --locked")
class AccountApiTests(unittest.TestCase):
    setUp = platform_fixture.TemplateApiTests.setUp
    start = platform_fixture.TemplateApiTests.start
    stop = platform_fixture.TemplateApiTests.stop
    request = platform_fixture.TemplateApiTests.request
    startup_failure = platform_fixture.TemplateApiTests.startup_failure

    # 管理员创建账号，客户端用密码登录；禁用和重置撤销旧会话，重启保留账号。
    def test_account_management_login_and_restart(self):
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

        def login(password):
            return self.request("POST", "/api/v1/client/auth/login", {"username": "SITE.USER", "password": password},
                                {"X-MskDSP-Action": "client-auth", "Cookie": ""})

        status, raw, _ = login("field-password-1")
        self.assertEqual(status, 200, raw)
        token = json.loads(raw)["access_token"]
        client = {"Authorization": "Bearer " + token, "Cookie": ""}
        self.assertEqual(self.request("GET", "/api/v1/client/auth/me", headers=client)[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/accounts", headers=client)[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/client/auth/me")[0], 401)
        status, raw, _ = self.request("PUT", path, {"revision": 1, "display_name": "现场维护", "enabled": False}, action)
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.request("GET", "/api/v1/client/auth/me", headers=client)[0], 401)
        self.assertEqual(login("field-password-1")[0], 401)
        self.assertEqual(self.request("PUT", path, {"revision": 2, "display_name": "现场维护", "enabled": True}, action)[0], 200)
        self.assertEqual(self.request("POST", path + "/password", {"revision": 3, "password": "field-password-2"}, action)[0], 200)
        self.assertEqual(login("field-password-1")[0], 401)
        status, raw, _ = login("field-password-2")
        self.assertEqual(status, 200, raw)
        before_restart = json.loads(raw)["access_token"]
        with closing(sqlite3.connect(self.folder / "data/accounts/accounts.sqlite3")) as db:
            password_hash, = db.execute("SELECT password_hash FROM accounts WHERE id=?", [account["id"]]).fetchone()
            self.assertTrue(password_hash.startswith("$argon2id$"))
            self.assertNotIn("field-password-2", password_hash)
        self.stop()
        self.start()
        self.assertEqual(self.request("GET", "/api/v1/client/auth/me", headers={"Authorization": "Bearer " + before_restart})[0], 401)
        self.assertEqual(login("field-password-2")[0], 200)
        status, raw, _ = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["revision"], 4)
        self.assertNotIn("password", raw.decode())
        self.assertNotIn("field-password", self.startup_log.read_text(encoding="utf-8"))

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

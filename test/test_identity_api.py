"""真实平台进程验证身份管理路由、运行目录、重启持久化及健康检查。"""
import json
from contextlib import closing
import sqlite3
import subprocess
import unittest

import test_modbus_api as platform_fixture

BINARY = platform_fixture.BINARY


@unittest.skipUnless(BINARY.exists(), "请先执行 cargo build --locked")
class IdentityApiTests(unittest.TestCase):
    setUp = platform_fixture.TemplateApiTests.setUp
    start = platform_fixture.TemplateApiTests.start
    stop = platform_fixture.TemplateApiTests.stop
    request = platform_fixture.TemplateApiTests.request
    startup_failure = platform_fixture.TemplateApiTests.startup_failure

    # 真实运行时注册页面和API，令牌仅显示一次，重启后保留资料、凭据摘要与历史。
    def test_real_runtime_identity_persistence_and_read_only_probe(self):
        action = {"X-MskDSP-Action": "agent-identity"}
        status, page, _ = self.request("GET", "/agents")
        self.assertEqual(status, 200)
        self.assertIn("上位机管理", page.decode())
        status, raw, headers = self.request("POST", "/api/v1/agents",
                                            {"name": "模拟现场电脑", "site_id": "site-a", "notes": "重启验证"}, action)
        self.assertEqual(status, 201)
        self.assertEqual(headers["cache-control"], "no-store")
        issued = json.loads(raw)
        agent_id, token = issued["agent"]["id"], issued["token"]
        self.assertEqual(len(token), 64)
        self.stop()
        self.start()
        status, raw, _ = self.request("GET", f"/api/v1/agents/{agent_id}")
        self.assertEqual(status, 200)
        detail = json.loads(raw)
        self.assertEqual(detail["name"], "模拟现场电脑")
        self.assertFalse(detail["online"])
        self.assertTrue(detail["credential_active"])
        self.assertNotIn(token, raw.decode())
        with closing(sqlite3.connect(self.folder / "data/identities/identities.sqlite3")) as db:
            digest, = db.execute("SELECT token_digest FROM agents WHERE id=?", [agent_id]).fetchone()
            self.assertEqual(len(digest), 32)
        probe = subprocess.run([str(BINARY), "--probe-grpc", f"127.0.0.1:{self.grpc_port}"],
                               capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        status, raw, _ = self.request("GET", "/readyz")
        self.assertEqual(json.loads(raw)["online_agents"], 0)
        status, raw, _ = self.request("POST", f"/api/v1/agents/{agent_id}/unbind", {"revision": 1}, action)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["credential_active"])
        status, raw, _ = self.request("GET", f"/api/v1/agents/{agent_id}/events")
        self.assertEqual([event["action"] for event in json.loads(raw)], ["unbound", "registered"])
        self.assertNotIn(token, self.startup_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

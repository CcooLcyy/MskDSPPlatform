import importlib.util
import http.client
import io
import json
import pathlib
import socketserver
import struct
import tempfile
import threading
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest import mock


MODULE = pathlib.Path(__file__).parents[1] / "script/grpc_network_host.py"
spec = importlib.util.spec_from_file_location("grpc_network_host", MODULE)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def config(**changes):
    value = dict(enabled=True, mss=1240, revision=1, port=50000,
                 external_interface="enp7s0", bridge_interface="docker0")
    value.update(changes)
    return value


class FakeNft:
    def __init__(self):
        self.current = None
        self.calls = []
        self.fail = False

    def apply(self, value):
        self.calls.append(dict(value))
        if self.fail:
            raise RuntimeError("模拟规则应用失败")
        self.current = dict(value) if value["enabled"] else None

    def inspect(self, value):
        return ((self.current == value if value["enabled"] else self.current is None), 7)

    def cleanup(self):
        self.current = None


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.directory.name) / "settings.json"
        self.path.write_text(json.dumps(config()), encoding="utf-8")
        self.nft = FakeNft()
        self.manager = host.Manager(self.path, self.nft)
        self.logger = mock.patch.object(host.LOG, "exception")
        self.logger.start()
        self.manager.start()

    def tearDown(self):
        self.directory.cleanup()
        self.logger.stop()

    # 启动恢复配置，状态仅在实际规则核验通过后报告生效。
    def test_start_and_actual_state(self):
        self.assertTrue(self.manager.status()["applied"])
        self.nft.current = None
        self.assertFalse(self.manager.status()["applied"])

    # 合法设置递增修订并写入原子持久文件，root 固定参数不变。
    def test_update_persists_revision(self):
        result = self.manager.update(dict(enabled=False, mss=1200, revision=1))
        self.assertEqual(result["revision"], 2)
        self.assertTrue(result["persistent"])
        self.assertTrue(result["applied"])
        self.assertEqual(json.loads(self.path.read_text())["port"], 50000)
        self.assertIsNone(self.nft.current)

    # 过期修订不能覆盖他人修改，也不执行规则命令。
    def test_stale_revision(self):
        with self.assertRaises(host.Conflict):
            self.manager.update(dict(enabled=False, mss=1240, revision=0))
        self.assertEqual(len(self.nft.calls), 1)

    # 拒绝布尔数值、浮点、越界和额外字段。
    def test_strict_request(self):
        base = dict(enabled=True, mss=1240, revision=1)
        for key, value in [("mss", True), ("revision", False), ("enabled", 1),
                           ("mss", 1240.0), ("mss", 535), ("mss", 1461),
                           ("revision", -1)]:
            with self.subTest(key=key, value=value), self.assertRaises(host.Invalid):
                self.manager.update(dict(base, **{key: value}))
        with self.assertRaises(host.Invalid):
            self.manager.update(dict(base, port=22))

    # 规则失败不得保存新配置或谎报生效。
    def test_nft_failure_does_not_save(self):
        before = self.path.read_bytes()
        self.nft.fail = True
        with self.assertRaises(host.Unavailable):
            self.manager.update(dict(enabled=False, mss=1240, revision=1))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.manager.status()["revision"], 1)

    # 持久化失败恢复原规则和旧修订。
    def test_persist_failure_rolls_back_rules(self):
        original = dict(self.manager.config)
        with mock.patch.object(host, "atomic_save", side_effect=[OSError("模拟磁盘错误"), None]):
            with self.assertRaises(host.Unavailable):
                self.manager.update(dict(enabled=False, mss=1200, revision=1))
        self.assertEqual(self.nft.current, original)
        self.assertEqual(self.manager.config, original)

    # 回退也失败时清楚报告不可用和未生效。
    def test_failed_rollback_is_not_applied(self):
        original_apply = self.nft.apply
        count = 0
        def fail_second(value):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("模拟回退错误")
            original_apply(value)
        self.nft.apply = fail_second
        with mock.patch.object(host, "atomic_save", side_effect=OSError("模拟磁盘错误")):
            with self.assertRaises(host.Unavailable):
                self.manager.update(dict(enabled=False, mss=1200, revision=1))
        self.assertFalse(self.manager.status()["applied"])
        self.assertFalse(self.manager.status()["persistent"])

    # 配置损坏时仍可查询错误，但禁止以网页请求覆盖 root 配置。
    def test_corrupt_configuration(self):
        self.path.write_text("{}")
        manager = host.Manager(self.path, self.nft)
        manager.start()
        self.assertFalse(manager.status()["applied"])
        self.assertFalse(manager.status()["persistent"])
        with self.assertRaises(host.Unavailable):
            manager.update(dict(enabled=True, mss=1240, revision=0))

    # 停止后延迟完成的请求不能重新安装已清理规则。
    def test_stop_rejects_delayed_update(self):
        self.manager.close()
        with self.assertRaises(host.Unavailable):
            self.manager.update(dict(enabled=True, mss=1240, revision=1))
        self.assertIsNone(self.nft.current)

    # 并发请求使用同一修订时仅一次保存成功。
    def test_concurrent_revision(self):
        results = []
        def run():
            try:
                self.manager.update(dict(enabled=True, mss=1200, revision=1))
                results.append("成功")
            except host.Conflict:
                results.append("冲突")
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(sorted(results), sorted(["成功", "冲突"]))

    # 原子替换失败必须保留原文件并删除临时正文。
    def test_atomic_replace_failure_preserves_file(self):
        before = self.path.read_bytes()
        with mock.patch.object(host.os, "replace", side_effect=OSError("模拟替换失败")):
            with self.assertRaises(OSError):
                host.atomic_save(self.path, config(mss=1200))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".grpc-network-*")), [])

    # 客户端在响应头或正文阶段关闭时，已提交设置保持成功且不二次发送 503。
    def test_committed_update_survives_disconnected_client(self):
        for exception in (BrokenPipeError, ConnectionResetError):
            for stage in ("headers", "body"):
                with self.subTest(exception=exception.__name__, stage=stage):
                    previous_revision = self.manager.config["revision"]
                    payload = json.dumps(dict(enabled=True, mss=1200, revision=previous_revision)).encode()
                    handler = host.Handler.__new__(host.Handler)
                    handler.path = "/settings"
                    handler.server = SimpleNamespace(manager=self.manager)
                    handler.authorized = mock.Mock(return_value=True)
                    handler.headers = Message()
                    handler.headers["Content-Length"] = str(len(payload))
                    handler.headers["Content-Type"] = "application/json"
                    handler.rfile = io.BytesIO(payload)
                    handler.wfile = mock.Mock()
                    handler.send_response = mock.Mock()
                    handler.send_header = mock.Mock()
                    handler.end_headers = mock.Mock()
                    failure = exception("模拟客户端关闭连接")
                    if stage == "headers":
                        handler.end_headers.side_effect = failure
                    else:
                        handler.wfile.write.side_effect = failure
                    with mock.patch.object(host.LOG, "warning") as warning:
                        handler.do_POST()
                    handler.send_response.assert_called_once_with(200)
                    warning.assert_called_once()
                    self.assertEqual(self.manager.config["revision"], previous_revision + 1)
                    self.assertEqual(json.loads(self.path.read_text()), self.manager.config)
                    self.assertEqual(self.nft.current, self.manager.config)
                    self.assertIsNone(self.manager.error)

    # 三阶段日志使用单调时钟分别记录毫秒，不把宿主机环境差异归因于磁盘。
    def test_update_logs_separate_phase_durations(self):
        with mock.patch.object(host.time, "monotonic", side_effect=[1, 1.002, 2, 2.003, 3, 3.004]):
            with self.assertLogs(host.LOG, level="INFO") as logged:
                self.manager.update(dict(enabled=True, mss=1200, revision=1))
        durations = [line for line in logged.output if "处理耗时" in line]
        self.assertEqual(len(durations), 3)
        for line, stage, milliseconds in zip(durations, ["应用规则", "持久化", "核验状态"], ["2.000", "3.000", "4.000"]):
            self.assertIn("阶段=" + stage, line)
            self.assertIn("耗时毫秒=" + milliseconds, line)


class RuleTests(unittest.TestCase):
    # 规则只操作独有表，并且只降低入站初始 SYN 的 MSS。
    def test_rule_scope(self):
        rule = host.render(config(), exists=True)
        self.assertIn("delete table ip mskdsp_grpc_mss", rule)
        self.assertNotIn("flush ruleset", rule)
        for token in ['iifname "enp7s0"', 'oifname "docker0"', "ct direction original",
                      "ct status dnat", "ct original proto-dst 50000", "tcp dport 50000",
                      "(syn | rst | ack) == syn", "maxseg size > 1240", "maxseg size set 1240"]:
            self.assertIn(token, rule)
        self.assertNotIn("drop", rule)

    # root 配置接口也必须校验，禁止拼接 nft 指令。
    def test_root_configuration_rejects_injection(self):
        bad = config()
        bad["external_interface"] = 'eth0"; flush ruleset;'
        with self.assertRaises(host.Invalid):
            host.validate_config(bad)
        bad = config()
        bad["port"] = 22
        with self.assertRaises(host.Invalid):
            host.validate_config(bad)

    # 停用规则只删本表，缺失时无操作。
    def test_disable_only_own_table(self):
        disabled = dict(config(), enabled=False)
        self.assertEqual(host.render(disabled, True), "delete table ip mskdsp_grpc_mss\n")
        self.assertEqual(host.render(disabled, False), "")

    # JSON 禁止重复键和非标准数值。
    def test_json_strict(self):
        for text in ['{"mss":1240,"mss":1200}', '{"mss":NaN}']:
            with self.assertRaises(host.Invalid):
                host.parse_json(text)

    # Unix peer 只允许 root 和配置的平台 UID。
    def test_peer_uid(self):
        self.assertTrue(host.peer_allowed(0, 10001))
        self.assertTrue(host.peer_allowed(10001, 10001))
        self.assertFalse(host.peer_allowed(10002, 10001))

    # SO_PEERCRED 读取失败或非白名单 UID 必须返回拒绝。
    def test_peer_credentials_fail_closed(self):
        handler = host.Handler.__new__(host.Handler)
        handler.server = mock.Mock(platform_uid=10001)
        handler.connection = mock.Mock()
        handler.respond = mock.Mock()
        with mock.patch.object(host.socket, "SO_PEERCRED", 17, create=True):
            for uid, allowed in [(0, True), (10001, True), (10002, False)]:
                handler.connection.getsockopt.return_value = struct.pack("3i", 1, uid, 10001)
                self.assertEqual(handler.authorized(), allowed)
            handler.connection.getsockopt.side_effect = OSError("模拟凭据读取失败")
            self.assertFalse(handler.authorized())
        self.assertEqual(handler.respond.call_args.args[0], 403)

    # nft 批次替换和实际 JSON 核验均只访问专属表。
    def test_nft_transaction_and_readback(self):
        nft = host.Nft()
        value = config()
        table = [{"table": {"family": "ip", "name": host.TABLE}},
                 {"chain": dict(name="forward", type="filter", hook="forward", prio=-150, policy="accept")},
                 {"rule": dict(chain="forward", expr=host.expected_expressions(value))}]
        with mock.patch.object(nft, "table", return_value=table), mock.patch.object(nft, "command") as command:
            nft.apply(value)
            self.assertEqual(command.call_count, 1)
            args, transaction = command.call_args.args
            self.assertEqual(args, ["-f", "-"])
            self.assertTrue(transaction.startswith("delete table ip " + host.TABLE))
        table[-1]["rule"]["expr"].append({"drop": None})
        with mock.patch.object(nft, "table", return_value=table):
            self.assertFalse(nft.inspect(value)[0])

    # nft 无法执行或失败必须向调用方报告，而不是假定成功。
    def test_nft_command_failure(self):
        with mock.patch.object(host.subprocess, "run", side_effect=OSError("模拟缺少命令")):
            with self.assertRaises(host.Unavailable):
                host.Nft().command(["list", "tables"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        class LocalHandler(host.Handler):
            # HTTP 编排测试使用内存管理器，真实 Unix 身份校验另有独立用例。
            def authorized(self):
                return True
        self.server = socketserver.TCPServer(("127.0.0.1", 0), LocalHandler)
        self.server.manager = mock.Mock()
        self.server.manager.status.return_value = dict(enabled=True, mss=1240, revision=1,
                                                       port=50000, persistent=True, applied=True,
                                                       matched_packets=0)
        self.server.manager.update.return_value = self.server.manager.status.return_value
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    # 本地 HTTP 状态和设置成功返回结构化 JSON。
    def test_http_contract(self):
        self.assertEqual(self.request("GET", "/status")[0], 200)
        self.assertEqual(self.request("POST", "/settings", '{"enabled":true,"mss":1240,"revision":1}',
                                      {"Content-Type": "application/json"})[0], 200)

    # 非法 JSON、额外路径和错误类型均不会进入设置逻辑。
    def test_http_reject_invalid(self):
        self.assertEqual(self.request("GET", "/status?extra=1")[0], 404)
        self.assertEqual(self.request("POST", "/settings", "{}", {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/settings", "{", {"Content-Type": "application/json"})[0], 400)
        self.server.manager.update.assert_not_called()

    # 修订冲突和宿主机失败分别保持明确 HTTP 错误码。
    def test_http_error_mapping(self):
        for error, code in [(host.Conflict("配置冲突"), 409), (host.Unavailable("规则失败"), 503),
                            (host.Invalid("参数错误"), 400)]:
            self.server.manager.update.side_effect = error
            self.assertEqual(self.request("POST", "/settings", "{}", {"Content-Type": "application/json"})[0], code)


if __name__ == "__main__":
    unittest.main()

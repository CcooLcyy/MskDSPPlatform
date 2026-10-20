"""验证真实平台 API 测试的启动等待、失败诊断与日志保留。"""
import http.client
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_modbus_api as api


class PlatformStartupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.runner = api.TemplateApiTests("runTest")
        self.runner.folder = self.root
        self.runner.config = self.root / "config.toml"
        self.runner.http_port, self.runner.grpc_port = 12345, 12346
        self.runner.cookie = "旧会话"
        self.runner.child = None
        self.child = mock.Mock()
        self.child.poll.return_value = None
        self.now = 0.0
        for patcher in (
            mock.patch.object(api, "ROOT", self.root),
            mock.patch.object(api.time, "monotonic", side_effect=lambda: self.now),
            mock.patch.object(api.time, "sleep", side_effect=self.advance),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(api.subprocess, "Popen", side_effect=self.launch)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.now += seconds

    def launch(self, *args, **kwargs):
        kwargs["stdout"].write("本次平台启动日志\n".encode())
        kwargs["stdout"].flush()
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        return self.child

    def ready(self, method, path, *args, **kwargs):
        if path == "/healthz":
            return 200, b"ok", {}
        return 200, b"{}", {"set-cookie": "session=new; HttpOnly"}

    # 启动超过旧的五秒限制仍能成功，健康探测短超时且登录只执行一次。
    def test_delayed_startup_succeeds_without_waiting_full_deadline(self):
        def respond(method, path, *args, **kwargs):
            if path == "/healthz":
                self.assertEqual(self.runner.cookie, "")
                self.assertLessEqual(kwargs["timeout"], 0.5)
                if self.now < 6:
                    raise ConnectionRefusedError()
            return self.ready(method, path, *args, **kwargs)

        with mock.patch.object(self.runner, "request", side_effect=respond) as request:
            self.runner.start()
        self.assertGreaterEqual(self.now, 6)
        self.assertLess(self.now, 6.2)
        self.assertEqual(sum(call.args[1] != "/healthz" for call in request.call_args_list), 1)
        self.assertEqual(self.runner.cookie, "session=new")

    # 总等待受单调时钟截止时间约束，最后一次探测与休眠不超过剩余时间。
    def test_timeout_reports_elapsed_ports_and_persistent_log(self):
        with mock.patch.object(api, "STARTUP_TIMEOUT", 0.25), \
             mock.patch.object(self.runner, "request", side_effect=TimeoutError("不应输出请求详情")) as request:
            with self.assertRaises(AssertionError) as failure:
                self.runner.start()
        self.assertAlmostEqual(self.now, 0.25)
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 0.5 for call in request.call_args_list))
        self.assertLess(request.call_args_list[-1].kwargs["timeout"], 0.1)
        for detail in ("未就绪", "0.25", "12345", "12346", "TimeoutError", "本次平台启动日志"):
            self.assertIn(detail, str(failure.exception))
        self.assertNotIn("不应输出请求详情", str(failure.exception))
        self.assertTrue(self.runner.startup_log.is_relative_to(self.root / "package" / "logs"))
        self.assertTrue(self.runner.startup_log.is_file())

    # 提前退出立即失败，保留退出码及日志，不继续请求 HTTP 接口。
    def test_early_exit_reports_return_code_and_log(self):
        self.child.poll.return_value = 23
        with mock.patch.object(self.runner, "request") as request:
            with self.assertRaises(AssertionError) as failure:
                self.runner.start()
        self.assertEqual(self.now, 0)
        request.assert_not_called()
        for detail in ("提前退出", "23", "本次平台启动日志", str(self.runner.startup_log)):
            self.assertIn(detail, str(failure.exception))

    # 探测操作累计耗时越过截止时间时，迟到的成功响应不能掩盖启动超时。
    def test_health_success_after_deadline_is_rejected(self):
        def late_response(*args, **kwargs):
            self.advance(31)
            return 200, b"ok", {}

        with mock.patch.object(self.runner, "request", side_effect=late_response) as request:
            with self.assertRaisesRegex(AssertionError, "平台 HTTP 未就绪"):
                self.runner.start()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.runner.cookie, "")

    # 服务启动期间的无效 HTTP 响应可重试，业务请求仍使用原有超时。
    def test_incomplete_http_startup_response_is_retried(self):
        with mock.patch.object(self.runner, "request", side_effect=[
            http.client.BadStatusLine("启动中"), (200, b"ok", {}),
            (200, b"{}", {"set-cookie": "session=new; HttpOnly"}),
        ]) as request:
            self.runner.start()
        self.assertEqual(request.call_count, 3)
        self.assertNotIn("timeout", request.call_args.kwargs)

    # 同一测试的多次启动分别保存日志，重启不会覆盖上一次输出。
    def test_restarts_preserve_separate_logs(self):
        with mock.patch.object(self.runner, "request", side_effect=self.ready):
            self.runner.start()
            first = self.runner.startup_log
            self.runner.stop()
            self.runner.start()
        self.assertNotEqual(first, self.runner.startup_log)
        self.assertIn("本次平台启动日志", first.read_text(encoding="utf-8"))
        self.assertIn("本次平台启动日志", self.runner.startup_log.read_text(encoding="utf-8"))

    # 登录拒绝立即失败并附启动日志，不重试或打印登录响应与会话凭据。
    def test_login_failure_is_not_retried_or_exposed(self):
        with mock.patch.object(self.runner, "request", side_effect=[
            (200, b"ok", {}), (401, b"private-response", {"set-cookie": "private-cookie"}),
        ]) as request:
            with self.assertRaises(AssertionError) as failure:
                self.runner.start()
        self.assertEqual(request.call_count, 2)
        self.assertIn("401", str(failure.exception))
        self.assertIn("本次平台启动日志", str(failure.exception))
        self.assertNotIn("private-response", str(failure.exception))
        self.assertNotIn("private-cookie", str(failure.exception))

    # 仅子进程的临时文件跟随夹具数据目录；启动日志仍保存在 CI 产物目录。
    def test_child_temp_directory_is_isolated_but_logs_remain_in_artifacts(self):
        with mock.patch.object(self.runner, "request", side_effect=self.ready):
            self.runner.start()
        env = self.popen.call_args.kwargs["env"]
        self.assertEqual(Path(env["TMPDIR"]), self.root / "tmp")
        self.assertTrue((self.root / "tmp").is_dir())
        self.assertEqual(self.runner.startup_log.parent, self.root / "package/logs/platform-startup")
        self.assertFalse(self.runner.startup_log.is_relative_to(self.root / "tmp"))

    # 进程忽略正常终止时强制回收，避免失败测试留下后台平台进程。
    def test_stop_kills_process_after_terminate_timeout(self):
        self.runner.child = self.child
        self.child.wait.side_effect = [subprocess.TimeoutExpired("platform", 5), 0]
        self.runner.stop()
        self.child.terminate.assert_called_once()
        self.child.kill.assert_called_once()
        self.assertEqual(self.child.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()

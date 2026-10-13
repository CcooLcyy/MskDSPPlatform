import copy
from contextlib import nullcontext
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import tarfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import runtime
from test_update_common import manifest


class FakeAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.command, self.path, dict(self.headers)))
        status, payload = self.server.auth_response
        if status is None:
            status, payload = ((200, {"username": "root", "role": "admin"})
                               if self.headers.get("Cookie") == "session=valid" and self.server.logged_in
                               else (401, {"error": "会话无效"}))
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.server.auth_checked.set()

    def do_POST(self):
        self.server.requests.append((self.command, self.path, dict(self.headers)))
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.logged_in = self.path.endswith("/login")
        self.send_response(200)
        self.send_header("Set-Cookie", "session=valid; HttpOnly" if self.server.logged_in else "session=; Max-Age=0")
        self.send_header("Set-Cookie", "preference=light")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.identity = {"service": "mskdsp-platform", "version": "0.0.1", "channel": "dev",
                         "commit": "unknown", "build_time": "unknown"}
        self.config = self.folder / "updater.json"
        self.config.write_text(json.dumps({"manifest_url": "https://updates.example/platform/ci/latest.json"}))
        self.platform_config = self.folder / "config.toml"
        self.platform_config.write_text('[server]\ngrpc_addr = "0.0.0.0:50000"\n')
        self.identity_mock = mock.patch.object(runtime, "binary_identity", return_value=self.identity)
        self.identity_mock.start()
        self.addCleanup(self.identity_mock.stop)
        self.manager = runtime.Manager(self.folder / "runtime", self.config, config=self.platform_config, seed=self.folder / "seed")
        self.addCleanup(self.manager.close)
        lock = mock.patch.object(runtime.common, "file_lock", return_value=nullcontext())
        lock.start()
        self.addCleanup(lock.stop)
        platform = mock.patch.object(runtime.common, "platform_key", return_value="linux-amd64")
        platform.start()
        self.addCleanup(platform.stop)

    def http_server(self, handler, manager=None):
        server = (runtime.RuntimeServer(("127.0.0.1", 0), manager) if manager
                  else ThreadingHTTPServer(("127.0.0.1", 0), handler))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def auth_backend(self):
        server = self.http_server(FakeAuthHandler)
        server.requests, server.auth_response, server.logged_in = [], (None, None), True
        server.auth_checked = threading.Event()
        self.manager.child_http = f"127.0.0.1:{server.server_port}"
        return server

    def http_request(self, server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def run_update(self, healthy=(True,)):
        data = manifest()
        candidate = self.manager.runtime_dir / "releases" / runtime.release_name(data["version"])
        candidate.mkdir()
        (candidate / "mskdsp-platform").write_bytes(b"candidate")
        with mock.patch.object(self.manager.transport, "fetch_manifest", return_value=data), \
             mock.patch.object(self.manager, "prepare", return_value=candidate), \
             mock.patch.object(self.manager, "launch") as launch, \
             mock.patch.object(self.manager, "stop_child") as stop, \
             mock.patch.object(self.manager, "healthy", side_effect=healthy):
            self.manager.run_job("apply")
            return launch.call_count, stop.call_count

    def test_success_commits_and_preserves_previous(self):
        launches, stops = self.run_update()
        self.assertEqual((launches, stops), (1, 1))
        state = json.loads((self.manager.runtime_dir / "state.json").read_text())
        self.assertEqual(state["previous"]["release"], "seed")
        self.assertEqual(self.manager.status()["state"], "succeeded")
        self.assertEqual(self.manager.current["version"], manifest()["version"])

    def test_failed_health_rolls_back_without_state_commit(self):
        launches, stops = self.run_update((False, True))
        self.assertEqual((launches, stops), (2, 2))
        self.assertEqual(self.manager.current["release"], "seed")
        self.assertFalse((self.manager.runtime_dir / "state.json").exists())
        self.assertIn("已回滚", self.manager.status()["message"])

    def test_failed_rollback_is_explicit(self):
        self.run_update((False, False))
        self.assertIn("回滚失败", self.manager.status()["message"])

    def test_validation_failure_keeps_old_child_and_busy_is_rejected(self):
        with mock.patch.object(self.manager.transport, "fetch_manifest", side_effect=runtime.common.UpdateError("下载失败")), \
             mock.patch.object(self.manager, "stop_child") as stop:
            self.manager.run_job("apply")
            stop.assert_not_called()
        self.manager.busy = True
        with self.assertRaises(runtime.BusyError):
            self.manager.begin("apply")

    def test_interrupted_task_recovered_and_same_version_does_not_restart(self):
        self.manager.set_result(state="updating")
        again = runtime.Manager(self.manager.runtime_dir, self.config, config=self.platform_config, seed=self.folder / "seed")
        self.assertEqual(again.status()["state"], "error")
        self.manager.current.update({key: manifest()[key] for key in runtime.IDENTITY_FIELDS})
        with mock.patch.object(self.manager.transport, "fetch_manifest", return_value=manifest()), \
             mock.patch.object(self.manager, "stop_child") as stop:
            self.manager.run_job("apply")
            stop.assert_not_called()
        self.assertEqual(self.manager.status()["state"], "current")

    def test_stop_terminates_kills_and_reaps(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [runtime.subprocess.TimeoutExpired("child", 15), 0]
        self.manager.child = child
        self.manager.stop_child()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_real_child_is_reaped_on_close(self):
        popen = runtime.subprocess.Popen
        def child_process(*args, **kwargs):
            return popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=child_process):
            self.manager.launch()
        child, reader = self.manager.child, self.manager.child_reader
        self.manager.close()
        self.assertIsNotNone(child.poll())
        self.assertIsNone(self.manager.child)
        self.assertFalse(reader.is_alive())
        self.assertTrue(child.stdout.closed)

    # 管理日志落盘保留中文，同时 Docker 控制台仍能看到生命周期信息。
    def test_manager_logs_are_persistent_and_visible_on_console(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as console:
            self.manager.log("测试管理进程日志")
        self.assertIn("测试管理进程日志", console.getvalue())
        self.assertIn("测试管理进程日志", (self.manager.runtime_dir / "logs" / "manager.log").read_text(encoding="utf-8"))

    # 管理日志长消息同样有界，有限历史不会随运行时间持续增长。
    def test_manager_large_messages_are_rotated(self):
        handler = self.manager.logs.manager.handlers[0]
        handler.maxBytes, handler.backupCount = 20 * 1024, 2
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            for _ in range(100):
                self.manager.log("管理日志" * 2048)
        paths = list((self.manager.runtime_dir / "logs").glob("manager.log*"))
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(path.stat().st_size <= 20 * 1024 for path in paths))

    # 大量子进程输出不能填满管道；无换行输出也受到轮换限制。
    def test_child_output_is_drained_and_rotated_with_bounded_files(self):
        handler = self.manager.logs.platform.handlers[0]
        handler.maxBytes, handler.backupCount = 20 * 1024, 2
        code = "import os; os.write(1, '平台中文日志\\n'.encode()); os.write(2, b'stderr-mark\\n'); os.write(1, b'x' * (1024 * 1024)); os.write(2, b'\\nfinal-mark\\n')"
        popen = runtime.subprocess.Popen
        def child_process(*args, **kwargs):
            return popen([sys.executable, "-c", code], **kwargs)
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=child_process), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as console:
            self.manager.launch()
            child, reader = self.manager.child, self.manager.child_reader
            self.assertEqual(child.wait(timeout=10), 0)
            self.manager.stop_child()
        self.assertFalse(reader.is_alive())
        self.assertTrue(child.stdout.closed)
        self.assertNotIn("final-mark", console.getvalue())
        paths = list((self.manager.runtime_dir / "logs").glob("platform.log*"))
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(path.stat().st_size <= 20 * 1024 for path in paths))
        self.assertIn("final-mark", (self.manager.runtime_dir / "logs" / "platform.log").read_text(encoding="utf-8"))

    # 标准输出和错误输出统一保存，UTF-8 多字节字符跨读取边界不会损坏。
    def test_child_stdout_stderr_and_split_utf8_are_saved(self):
        raw = io.BytesIO(("x" * 4094 + "中文输出\n错误输出\n").encode("utf-8"))
        self.manager.logs.capture(raw)
        contents = (self.manager.runtime_dir / "logs" / "platform.log").read_text(encoding="utf-8")
        payload = "".join(line.split("] ", 1)[1] for line in contents.splitlines())
        self.assertIn("中文输出", payload)
        self.assertIn("错误输出", contents)
        self.assertNotIn("\ufffd", contents)
        self.assertTrue(raw.closed)

    # 真实子进程的标准输出与错误输出都进入同一持久日志。
    def test_real_child_stdout_and_stderr_are_saved(self):
        popen = runtime.subprocess.Popen
        def child_process(*args, **kwargs):
            return popen([sys.executable, "-c", "import os; os.write(1, '标准输出\\n'.encode()); os.write(2, '错误输出\\n'.encode())"], **kwargs)
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=child_process):
            self.manager.launch()
        self.assertEqual(self.manager.child.wait(timeout=10), 0)
        self.manager.stop_child()
        contents = (self.manager.runtime_dir / "logs" / "platform.log").read_text(encoding="utf-8")
        self.assertIn("标准输出", contents)
        self.assertIn("错误输出", contents)

    # 关闭后迟到的工作线程日志不能重新打开文件句柄。
    def test_logs_close_releases_files_and_late_logs_do_not_reopen(self):
        self.manager.log("退出前日志")
        self.manager.close()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.manager.log("退出后迟到日志")
        self.assertTrue(all(handler.stream is None for logger in (self.manager.logs.manager, self.manager.logs.platform)
                            for handler in logger.handlers))
        self.assertNotIn("退出后迟到日志", (self.manager.runtime_dir / "logs" / "manager.log").read_text(encoding="utf-8"))

    # 创建子进程失败不能留下活动的日志读取线程。
    def test_failed_child_launch_does_not_leave_reader(self):
        with mock.patch.object(runtime.subprocess, "Popen", side_effect=OSError("程序不存在")):
            with self.assertRaises(OSError):
                self.manager.launch()
        self.assertIsNone(self.manager.child)
        self.assertIsNone(self.manager.child_reader)

    # 日志线程启动失败时立即杀死并回收子进程，关闭管道句柄。
    def test_failed_reader_start_reaps_child_and_closes_pipe(self):
        child = mock.Mock()
        with mock.patch.object(runtime.subprocess, "Popen", return_value=child), \
             mock.patch.object(runtime.threading.Thread, "start", side_effect=RuntimeError("线程无法启动")):
            with self.assertRaises(RuntimeError):
                self.manager.launch()
        child.kill.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)
        child.stdout.close.assert_called_once()
        self.assertIsNone(self.manager.child)
        self.assertIsNone(self.manager.child_reader)

    def test_prepare_downloads_verifies_and_cleans_temporary_files(self):
        data = manifest()
        binary = bytearray(64)
        binary[:6] = b"\x7fELF\x02\x01"
        binary[18:20] = (62).to_bytes(2, "little")
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            item = tarfile.TarInfo("mskdsp-platform")
            item.size = len(binary)
            archive.addfile(item, io.BytesIO(binary))
        raw = payload.getvalue()
        artifact = data["platforms"]["linux-amd64"]
        artifact.update(binary_size=len(raw), binary_sha256=hashlib.sha256(raw).hexdigest())
        identity = {"service": "mskdsp-platform", **{key: data[key] for key in runtime.IDENTITY_FIELDS}}
        with mock.patch.object(self.manager.transport, "open", return_value=io.BytesIO(raw)), \
             mock.patch.object(runtime, "binary_identity", return_value=identity), \
             mock.patch.object(self.manager, "stop_child") as stop:
            candidate = self.manager.prepare(data, artifact)
            self.assertEqual((candidate / "mskdsp-platform").read_bytes(), bytes(binary))
            self.assertEqual(list(self.manager.runtime_dir.glob(".download-*")), [])
            stop.assert_not_called()

    def test_identity_mismatch_rejected(self):
        with self.assertRaises(runtime.common.UpdateError):
            runtime.verify_candidate(self.folder / "binary", manifest())

    def test_grpc_probe_follows_configuration(self):
        config = self.folder / "config.toml"
        for address, expected in (("0.0.0.0:55051", "127.0.0.1:55051"), ("[::]:55052", "[::1]:55052"), ("127.0.0.1:50000", "127.0.0.1:50000")):
            config.write_text(f'[server]\ngrpc_addr = "{address}"\n')
            self.assertEqual(runtime.probe_address(config), expected)
        manager = runtime.Manager(self.folder / "separate", self.config, config=config, grpc_probe="127.0.0.1:56051")
        self.assertEqual(manager.grpc_probe, "127.0.0.1:56051")

    def test_committed_install_is_not_rolled_back_when_task_result_write_fails(self):
        original = self.manager.set_result
        def set_result(**fields):
            if fields.get("state") == "succeeded":
                raise OSError("任务结果无法保存")
            original(**fields)
        with mock.patch.object(self.manager, "set_result", side_effect=set_result):
            launches, stops = self.run_update()
        self.assertEqual((launches, stops), (1, 1))
        self.assertEqual(self.manager.current["version"], manifest()["version"])
        self.assertIn("新版已提交", self.manager.status()["message"])

    def test_committed_restart_keeps_new_binary_and_missing_binary_is_rejected(self):
        self.run_update()
        identity = {"service": "mskdsp-platform", **{key: manifest()[key] for key in runtime.IDENTITY_FIELDS}}
        with mock.patch.object(runtime, "binary_identity", return_value=identity), mock.patch.object(runtime.common, "verify_elf"):
            again = runtime.Manager(self.manager.runtime_dir, self.config, config=self.platform_config, seed=self.folder / "seed")
            self.assertEqual(again.current["version"], manifest()["version"])
            self.assertEqual(again.binary_path().read_bytes(), b"candidate")
            again.binary_path().unlink()
            with self.assertRaises(runtime.common.UpdateError):
                runtime.Manager(self.manager.runtime_dir, self.config, config=self.platform_config, seed=self.folder / "seed")

    def test_bounded_supervision_stops_after_three_restarts(self):
        event = mock.Mock()
        event.wait.side_effect = [False, False, False, False, True]
        event.is_set.return_value = False
        self.manager.stopping = event
        child = mock.Mock()
        child.poll.return_value = 1
        def launch():
            self.manager.child = child
        self.manager.child = child
        with mock.patch.object(self.manager, "launch", side_effect=launch) as start, \
             mock.patch.object(self.manager, "healthy", return_value=True):
            self.manager.supervise()
        self.assertEqual(start.call_count, 3)
        self.assertIsNone(self.manager.child)
        self.assertIn("停止自动重启", self.manager.status()["message"])

    def test_real_http_proxy_and_health_requires_identity_and_rpc(self):
        identity = self.identity
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = identity if self.path == "/api/v1/version" else {"status": "ready"}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(201)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        self.manager.child_http = f"127.0.0.1:{backend.server_port}"
        self.manager.child = mock.Mock()
        self.manager.child.poll.return_value = None
        with mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0)) as probe:
            self.assertTrue(self.manager.healthy(timeout=1))
            self.assertEqual(probe.call_args.args[0][-2:], ["--probe-grpc", "127.0.0.1:50000"])
            self.assertEqual(probe.call_args.kwargs["env"]["MSKDSP_PLATFORM_CONFIG"], str(self.platform_config))
        with mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(self.manager.healthy(timeout=0.05))
        identity["commit"] = "wrong"
        with mock.patch.object(runtime.subprocess, "run") as probe:
            self.assertFalse(self.manager.healthy(timeout=0.05))
            probe.assert_not_called()
        server = runtime.RuntimeServer(("127.0.0.1", 0), self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("POST", "/echo", b"payload")
        response = connection.getresponse()
        self.assertEqual(response.status, 201)
        self.assertEqual(response.read(), b"payload")
        connection.close()

    def test_http_contract_and_request_constraints(self):
        # 使用真实已验证会话执行原有接口契约，保持认证检查启用。
        self.auth_backend()
        server = runtime.RuntimeServer(("127.0.0.1", 0), self.manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection(host, port, timeout=3)
            connection.request(method, path, body, {"Cookie": "session=valid", **(headers or {})})
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
            return status
        self.assertEqual(request("GET", "/api/update/status"), 200)
        self.assertEqual(request("GET", "/api/update/apply"), 405)
        self.assertEqual(request("POST", "/api/update/apply"), 403)
        marked = {"X-MskDSP-Action": "update"}
        self.assertEqual(request("POST", "/api/update/apply", '{"url":"x"}', marked), 400)
        self.assertEqual(request("POST", "/api/update/apply", " " * 1025, marked), 413)
        self.assertEqual(request("POST", "/api/update/apply", "{}", {**marked, "Origin": "https://evil.example"}), 403)
        with mock.patch.object(self.manager, "begin") as begin:
            self.assertEqual(request("POST", "/api/update/check", "{}", marked), 202)
            begin.assert_called_once_with("check")
        self.manager.busy = True
        self.assertEqual(request("POST", "/api/update/apply", "{}", marked), 409)

    # 更新接口全部先校验会话，伪造身份头不能读取状态或启动任务。
    def test_update_routes_require_cookie_session(self):
        backend = self.auth_backend()
        server = self.http_server(None, self.manager)
        forged = {"Authorization": "Bearer root", "X-Forwarded-User": "root",
                  "X-Forwarded-Host": "trusted.example", "X-MskDSP-Action": "update"}
        with mock.patch.object(self.manager, "begin") as begin:
            for method, path in (("GET", "/api/update/status"), ("POST", "/api/update/check"),
                                 ("POST", "/api/update/apply"), ("GET", "/api/update/unknown"),
                                 ("GET", "/api/update/status?x=1")):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.http_request(server, method, path, "{}", forged)[0], 401)
            self.assertEqual(self.http_request(server, "POST", "/api/update/apply", "{}",
                                               {**forged, "Cookie": "session=invalid"})[0], 401)
            begin.assert_not_called()
        for method, path, headers in backend.requests:
            self.assertEqual((method, path), ("GET", "/api/v1/auth/me"))
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("X-Forwarded-User", headers)

    # 登录/注销经代理传递 Cookie；注销后原 Cookie 也不能继续更新。
    def test_auth_proxy_preserves_host_cookies_and_logout_revokes_update(self):
        backend = self.auth_backend()
        backend.logged_in = False
        server = self.http_server(None, self.manager)
        headers = {"Host": "platform.example:8080", "Origin": "http://platform.example:8080",
                   "X-Forwarded-Host": "forged.example", "X-Forwarded-User": "root"}
        status, response_headers, _ = self.http_request(server, "POST", "/api/v1/auth/login", "{}", headers)
        self.assertEqual(status, 200)
        self.assertEqual([value for name, value in response_headers if name.lower() == "set-cookie"],
                         ["session=valid; HttpOnly", "preference=light"])
        forwarded = backend.requests[-1][2]
        self.assertEqual(forwarded["Host"], "platform.example:8080")
        self.assertEqual(forwarded["Origin"], headers["Origin"])
        self.assertNotIn("X-Forwarded-Host", forwarded)
        authenticated = {**headers, "Cookie": "session=valid", "X-MskDSP-Action": "update"}
        self.assertEqual(self.http_request(server, "GET", "/api/update/status", headers=authenticated)[0], 200)
        self.assertEqual(backend.requests[-1][2]["Cookie"], "session=valid")
        with mock.patch.object(self.manager, "begin") as begin:
            self.assertEqual(self.http_request(server, "POST", "/api/update/check", "{}", authenticated)[0], 202)
            begin.assert_called_once_with("check")
        status, response_headers, _ = self.http_request(server, "POST", "/api/v1/auth/logout", "{}", authenticated)
        self.assertEqual(status, 200)
        self.assertIn(("Set-Cookie", "session=; Max-Age=0"), response_headers)
        self.assertEqual(backend.requests[-1][2]["Cookie"], "session=valid")
        with mock.patch.object(self.manager, "begin") as begin:
            self.assertEqual(self.http_request(server, "POST", "/api/update/apply", "{}", authenticated)[0], 401)
            begin.assert_not_called()

    # 子进程不可达或身份响应异常时拒绝更新，不得降级为未认证执行。
    def test_auth_unavailable_or_invalid_response_fails_closed(self):
        backend = self.auth_backend()
        server = self.http_server(None, self.manager)
        headers = {"Cookie": "session=valid", "X-MskDSP-Action": "update"}
        invalid = ((500, {"error": "失败"}), (302, {}), (200, b"not json"),
                   (200, b"\xff"), (200, b"[" * 2000 + b"]" * 2000),
                   (200, []), (200, {"username": "root"}),
                   (200, {"username": " ", "role": "admin"}),
                   (200, {"username": 1, "role": "admin"}),
                   (200, {"username": "root", "role": "viewer"}),
                   (200, b" " * 4097), (401, b"not json"))
        with mock.patch.object(self.manager, "begin") as begin:
            for reply in invalid:
                with self.subTest(reply=reply):
                    backend.auth_response = reply
                    self.assertEqual(self.http_request(server, "POST", "/api/update/apply", "{}", headers)[0], 503)
            backend.shutdown()
            backend.server_close()
            self.assertEqual(self.http_request(server, "GET", "/api/update/status", headers=headers)[0], 503)
            self.assertEqual(self.http_request(server, "POST", "/api/update/apply", "{}", headers)[0], 503)
            begin.assert_not_called()

    # Rust 可配置管理员用户名，更新校验必须接受服务端验证过的非空用户名。
    def test_authenticated_custom_username_can_update(self):
        backend = self.auth_backend()
        backend.auth_response = (200, {"username": "custom-admin", "role": "admin"})
        server = self.http_server(None, self.manager)
        with mock.patch.object(self.manager, "begin") as begin:
            status, _, _ = self.http_request(server, "POST", "/api/update/check", "{}",
                                             {"Cookie": "session=valid", "X-MskDSP-Action": "update"})
            self.assertEqual(status, 202)
            begin.assert_called_once_with("check")

    # 请求体慢速上传期间注销，提交前再次验证会话，禁止启动更新任务。
    def test_logout_during_body_upload_prevents_update(self):
        backend = self.auth_backend()
        server = self.http_server(None, self.manager)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        self.addCleanup(connection.close)
        with mock.patch.object(self.manager, "begin") as begin:
            connection.putrequest("POST", "/api/update/apply")
            connection.putheader("Content-Length", "2")
            connection.putheader("Cookie", "session=valid")
            connection.putheader("X-MskDSP-Action", "update")
            connection.endheaders()
            self.assertTrue(backend.auth_checked.wait(timeout=2), "请求体上传前未完成首次会话验证")
            backend.logged_in = False
            connection.send(b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            begin.assert_not_called()
        self.assertEqual([path for _, path, _ in backend.requests],
                         ["/api/v1/auth/me", "/api/v1/auth/me"])


if __name__ == "__main__":
    unittest.main()

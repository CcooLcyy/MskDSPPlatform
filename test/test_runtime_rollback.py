import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

import test_runtime as support

runtime = support.runtime


class RollbackTests(unittest.TestCase):
    setUp = support.ManagerTests.setUp
    http_server = support.ManagerTests.http_server
    auth_backend = support.ManagerTests.auth_backend
    http_request = support.ManagerTests.http_request

    def installed_pair(self):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = (62).to_bytes(2, "little")
        self.manager.seed.write_bytes(header)
        previous = {**self.identity, "release": "seed"}
        data = support.manifest()
        current = {key: data[key] for key in runtime.IDENTITY_FIELDS}
        current.update(release=runtime.release_name(data["version"]), platform="linux-amd64",
                       binary_sha256=hashlib.sha256(header + b"new").hexdigest(), previous=previous)
        self.manager.current = current
        path = self.manager.binary_path()
        path.parent.mkdir()
        path.write_bytes(header + b"new")
        runtime.common.atomic_json(self.manager.runtime_dir / "state.json", current)
        return copy.deepcopy(current), copy.deepcopy(previous)

    def run_rollback(self, healthy=(True,)):
        with mock.patch.object(self.manager, "launch") as launch, \
             mock.patch.object(self.manager, "stop_child") as stop, \
             mock.patch.object(self.manager, "healthy", side_effect=healthy), \
             mock.patch.object(self.manager.transport, "fetch_manifest", side_effect=AssertionError("回滚不访问更新源")) as fetch:
            self.manager.run_job("rollback")
            fetch.assert_not_called()
        return launch, stop

    # 无上一版或本地程序缺失时不提供回滚；任务忙时保留互斥。
    def test_status_and_begin_reject_missing_previous_and_busy(self):
        self.assertEqual(self.manager.status()["previous_version"], "")
        self.assertFalse(self.manager.status()["rollback_available"])
        with self.assertRaises(runtime.common.UpdateError):
            self.manager.begin("rollback", target_version="0.0.1")
        self.installed_pair()
        self.assertTrue(self.manager.status()["rollback_available"])
        self.assertEqual(self.manager.status()["previous_version"], "0.0.1")
        self.manager.seed.unlink()
        self.assertFalse(self.manager.status()["rollback_available"])
        self.manager.busy = True
        with self.assertRaises(runtime.BusyError):
            self.manager.begin("rollback", target_version="0.0.1")

    # 目标已变化时拒绝旧页面确认，且不创建任务。
    def test_stale_target_is_rejected_before_task(self):
        self.installed_pair()
        with mock.patch.object(runtime.threading, "Thread") as thread:
            with self.assertRaises(runtime.common.UpdateError):
                self.manager.begin("rollback", target_version="other")
            thread.assert_not_called()
        self.assertFalse(self.manager.busy)

    # 回滚独立于更新源，成功后持久化并清除失败版本的回滚指针。
    def test_success_without_source_commits_previous_and_survives_restart(self):
        current, previous = self.installed_pair()
        self.manager.source_url = ""
        self.assertFalse(self.manager.status()["enabled"])
        self.assertTrue(self.manager.status()["rollback_available"])
        self.manager.busy = True
        launch, stop = self.run_rollback()
        launch.assert_called_once_with(previous)
        stop.assert_called_once()
        self.assertEqual(self.manager.current, previous)
        self.assertEqual(self.manager.status()["state"], "rolled_back")
        self.assertFalse(self.manager.status()["rollback_available"])
        self.assertFalse(self.manager.status()["update_available"])
        self.assertEqual(self.manager.status()["latest_version"], "")
        self.assertFalse(self.manager.busy)
        self.assertFalse((self.manager.runtime_dir / "releases" / current["release"]).exists())
        again = runtime.Manager(self.manager.runtime_dir, self.config, config=self.platform_config,
                                seed=self.manager.seed)
        self.addCleanup(again.close)
        self.assertEqual(again.current, previous)
        self.assertEqual(again.status()["state"], "rolled_back")

    # 普通已安装版本用可执行文件摘要校验，不能误用压缩包摘要；回退后仍可再次手动升级。
    def test_release_rollback_verifies_executable_and_can_update_again(self):
        current, _ = self.installed_pair()
        data = support.manifest(version="0.0.1-ci.20261012t080000.11.1+sha.abcdef1")
        data["build_time"] = "2026-10-12T08:00:00Z"
        previous = {key: data[key] for key in runtime.IDENTITY_FIELDS}
        previous.update(release=runtime.release_name(data["version"]), platform="linux-amd64",
                        binary_sha256="a" * 64, executable_sha256=hashlib.sha256(self.elf()).hexdigest())
        path = self.manager.binary_path(previous)
        path.parent.mkdir()
        path.write_bytes(self.elf())
        self.manager.current["previous"] = previous
        with mock.patch.object(runtime, "binary_identity", return_value={"service": "mskdsp-platform", **previous}):
            launch, _ = self.run_rollback()
            launch.assert_called_once_with(previous)
            again = runtime.Manager(self.manager.runtime_dir, self.config, config=self.platform_config,
                                    seed=self.manager.seed)
        self.addCleanup(again.close)
        self.assertEqual(again.current, previous)
        self.assertFalse((self.manager.runtime_dir / "releases" / current["release"]).exists())
        support.ManagerTests.run_update(self)
        self.assertEqual(self.manager.status()["state"], "succeeded")
        self.assertEqual(self.manager.current["previous"], previous)
        self.assertEqual(self.manager.current["executable_sha256"], hashlib.sha256(b"candidate").hexdigest())

    # 真实后台线程接受没有更新源的回滚，完成后释放互斥。
    def test_begin_runs_rollback_without_update_source(self):
        _, previous = self.installed_pair()
        self.manager.source_url = ""
        with mock.patch.object(self.manager, "launch"), mock.patch.object(self.manager, "stop_child"), \
             mock.patch.object(self.manager, "healthy", return_value=True):
            self.manager.begin("rollback", target_version=previous["version"])
            self.manager.worker.join(timeout=3)
        self.assertFalse(self.manager.worker.is_alive())
        self.assertFalse(self.manager.busy)
        self.assertEqual(self.manager.status()["state"], "rolled_back")

    # 新升级为上一版本保存独立的程序摘要，后续文件损坏在停止当前进程前被发现。
    def test_update_records_previous_digest_and_rejects_corruption(self):
        self.manager.seed.write_bytes(self.elf())
        support.ManagerTests.run_update(self)
        previous = self.manager.current["previous"]
        self.assertEqual(previous["executable_sha256"], hashlib.sha256(self.elf()).hexdigest())
        self.manager.seed.write_bytes(self.elf() + b"changed")
        launch, stop = self.run_rollback()
        launch.assert_not_called()
        stop.assert_not_called()
        self.assertIn("SHA256", self.manager.status()["message"])

    # 管理进程退出时保留最后提交状态，由重启恢复，不在退出期间重新拉起进程。
    def test_shutdown_during_rollback_keeps_committed_current(self):
        current, _ = self.installed_pair()

        def shutdown(*args):
            self.manager.stopping.set()
            return True

        with mock.patch.object(self.manager, "launch") as launch, \
             mock.patch.object(self.manager, "stop_child") as stop, \
             mock.patch.object(self.manager, "healthy", side_effect=shutdown):
            self.manager.run_job("rollback")
        self.manager.stopping.clear()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(self.manager.current, current)
        self.assertEqual(json.loads((self.manager.runtime_dir / "state.json").read_text()), current)
        self.assertIn("取消", self.manager.status()["message"])

    # 目标启动失败恢复回滚前版本，且不修改已提交的安装状态。
    def test_failed_target_restores_current_and_reports_restore_failure(self):
        current, previous = self.installed_pair()
        state_path = self.manager.runtime_dir / "state.json"
        for healthy, message in (((False, True), "已恢复"), ((False, False), "恢复失败")):
            with self.subTest(healthy=healthy):
                launch, stop = self.run_rollback(healthy)
                self.assertEqual(launch.call_args_list, [mock.call(previous), mock.call(current)])
                self.assertEqual(stop.call_count, 2)
                self.assertEqual(self.manager.current, current)
                self.assertEqual(json.loads(state_path.read_text()), current)
                self.assertEqual(self.manager.status()["state"], "error")
                self.assertIn(message, self.manager.status()["message"])

    # 安装状态落盘失败恢复当前版；成功落盘后任务提示写入失败不撤销回滚。
    def test_state_commit_failure_and_post_commit_result_failure(self):
        current, previous = self.installed_pair()
        atomic = runtime.common.atomic_json

        def fail_state(path, value):
            if Path(path).name == "state.json":
                raise OSError("安装状态写入失败")
            atomic(path, value)

        with mock.patch.object(runtime.common, "atomic_json", side_effect=fail_state):
            launch, stop = self.run_rollback((True, True))
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(self.manager.current, current)
        self.assertIn("已恢复", self.manager.status()["message"])
        original = self.manager.set_result

        def fail_result(**fields):
            if fields.get("state") == "rolled_back":
                raise OSError("任务状态写入失败")
            original(**fields)

        with mock.patch.object(self.manager, "set_result", side_effect=fail_result):
            launch, stop = self.run_rollback()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.manager.current, previous)
        saved = json.loads((self.manager.runtime_dir / "state.json").read_text())
        self.assertEqual(saved, previous)
        self.assertIn("已提交", self.manager.status()["message"])

    # 回滚前校验路径、ELF、身份与摘要，异常时当前进程保持运行。
    def test_invalid_previous_never_stops_current(self):
        current, previous = self.installed_pair()
        for kind in ("missing", "path", "identity", "elf", "checksum", "linked"):
            with self.subTest(kind=kind):
                self.manager.current = copy.deepcopy(current)
                self.manager.seed.write_bytes(b"invalid" if kind == "elf" else self.elf())
                prior = self.manager.current["previous"]
                if kind == "missing":
                    self.manager.seed.unlink()
                elif kind == "path":
                    prior["release"] = "../../outside"
                elif kind == "identity":
                    prior["version"] = "0.0.2"
                elif kind == "checksum":
                    prior["executable_sha256"] = "0" * 64
                with mock.patch.object(Path, "is_symlink", return_value=kind == "linked"):
                    launch, stop = self.run_rollback()
                stop.assert_not_called()
                launch.assert_not_called()
                self.assertEqual(self.manager.status()["state"], "error")

    def elf(self):
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = (62).to_bytes(2, "little")
        return header

    # 新增忙状态也按中断任务恢复；回滚不改变运行期监督策略。
    def test_interrupted_rollback_is_recovered(self):
        current, _ = self.installed_pair()
        self.manager.set_result(state="rolling_back")
        identity = {"service": "mskdsp-platform", **{key: current[key] for key in runtime.IDENTITY_FIELDS}}
        with mock.patch.object(runtime, "binary_identity", return_value=identity):
            again = runtime.Manager(self.manager.runtime_dir, self.config, config=self.platform_config,
                                    seed=self.manager.seed)
        self.addCleanup(again.close)
        self.assertEqual(again.current, current)
        self.assertEqual(again.status()["state"], "error")
        self.assertIn("中断", again.status()["message"])

    # 管理员确认的目标由接口传入；认证、方法、同源与参数校验仍生效。
    def test_http_rollback_auth_target_and_request_contract(self):
        self.installed_pair()
        self.auth_backend()
        server = self.http_server(None, self.manager)
        path = "/api/update/rollback"
        headers = {"Cookie": "session=valid", "X-MskDSP-Action": "update"}
        payload = json.dumps({"target_version": "0.0.1"})
        with mock.patch.object(self.manager, "begin") as begin:
            self.assertEqual(self.http_request(server, "POST", path, payload)[0], 401)
            self.assertEqual(self.http_request(server, "GET", path, headers=headers)[0], 405)
            self.assertEqual(self.http_request(server, "POST", path, payload,
                                               {"Cookie": "session=valid"})[0], 403)
            self.assertEqual(self.http_request(server, "POST", path, payload,
                                               {**headers, "Origin": "https://evil.example"})[0], 403)
            for body in ("{}", "[]", '{"target_version":1}', '{"target_version":""}',
                         '{"target_version":"0.0.1","path":"x"}'):
                self.assertEqual(self.http_request(server, "POST", path, body, headers)[0], 400)
            begin.assert_not_called()
            self.assertEqual(self.http_request(server, "POST", path, payload, headers)[0], 202)
            begin.assert_called_once_with("rollback", target_version="0.0.1")
        self.assertEqual(self.http_request(server, "POST", path,
                                           '{"target_version":"changed"}', headers)[0], 409)

    # 请求体上传时注销会话，不能继续提交回滚。
    def test_logout_during_rollback_request_is_rechecked(self):
        self.installed_pair()
        backend = self.auth_backend()
        server = self.http_server(None, self.manager)
        connection = support.http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        self.addCleanup(connection.close)
        payload = b'{"target_version":"0.0.1"}'
        with mock.patch.object(self.manager, "begin") as begin:
            connection.putrequest("POST", "/api/update/rollback")
            connection.putheader("Content-Length", str(len(payload)))
            connection.putheader("Cookie", "session=valid")
            connection.putheader("X-MskDSP-Action", "update")
            connection.endheaders()
            self.assertTrue(backend.auth_checked.wait(timeout=2))
            backend.logged_in = False
            connection.send(payload)
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            begin.assert_not_called()


if __name__ == "__main__":
    unittest.main()

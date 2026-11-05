"""平台每分钟自动检查与安装的调度和进程切换验收。"""
import copy
import json
import threading
import unittest
from unittest import mock

import test_runtime as support

runtime = support.runtime


class AutoUpdateTests(unittest.TestCase):
    setUp = support.ManagerTests.setUp
    local_manager = support.ManagerTests.local_manager

    def run_ticks(self, count, begin=None):
        with mock.patch.object(self.manager.stopping, "wait", side_effect=[False] * count + [True]) as wait:
            if begin is None:
                self.manager.auto_update_loop()
            else:
                with mock.patch.object(self.manager, "begin", side_effect=begin):
                    self.manager.auto_update_loop()
        return wait

    # 第一次检查必须先等待六十秒，后续每轮仍等待六十秒且直接提交安装任务。
    def test_first_and_subsequent_checks_wait_one_minute(self):
        events = []
        ticks = iter([False, False, True])

        def wait(seconds):
            self.assertEqual(seconds, 60)
            events.append("等待")
            return next(ticks)

        def begin(operation):
            self.assertEqual(operation, "apply")
            events.append("检查并安装")

        self.assertEqual(runtime.AUTO_UPDATE_INTERVAL, 60)
        with mock.patch.object(self.manager.stopping, "wait", side_effect=wait), \
             mock.patch.object(self.manager, "begin", side_effect=begin):
            self.manager.auto_update_loop()
        self.assertEqual(events, ["等待", "检查并安装", "等待", "检查并安装", "等待"])

    # 更新源缺失时状态明确禁用自动更新，定时轮次不得创建安装任务。
    def test_missing_source_disables_automatic_jobs(self):
        self.manager.source_url = ""
        self.assertEqual(self.manager.status()["auto_update_interval_seconds"], 0)
        with mock.patch.object(self.manager, "begin") as begin:
            self.run_ticks(2)
        begin.assert_not_called()

    # 本地软件库和远端来源都向页面声明每分钟自动检查。
    def test_status_reports_interval_for_both_sources(self):
        self.assertEqual(self.manager.status()["auto_update_interval_seconds"], 60)
        manager = self.local_manager()
        self.assertEqual(manager.status()["auto_update_interval_seconds"], 60)

    # 手动检查、安装或回滚占用互斥时跳过本轮，释放后下一分钟可继续。
    def test_busy_job_is_not_duplicated_and_next_tick_recovers(self):
        self.manager.busy = True
        ticks = 0

        def wait(seconds):
            nonlocal ticks
            self.assertEqual(seconds, 60)
            ticks += 1
            if ticks == 2:
                self.manager.busy = False
            return ticks == 3

        with mock.patch.object(self.manager.stopping, "wait", side_effect=wait), \
             mock.patch.object(runtime.threading, "Thread") as worker:
            self.manager.auto_update_loop()
        self.assertEqual(worker.call_count, 1)
        worker.return_value.start.assert_called_once()
        self.assertTrue(self.manager.busy)
        self.manager.worker = None
        self.manager.busy = False

    # 提交任务失败不能终止常驻调度，下一轮应继续检查且记录错误。
    def test_submission_error_does_not_stop_next_check(self):
        with mock.patch.object(self.manager, "begin", side_effect=[OSError("状态文件不可写"), None]) as begin, \
             mock.patch.object(self.manager, "log") as log:
            self.run_ticks(2)
        self.assertEqual(begin.call_args_list, [mock.call("apply"), mock.call("apply")])
        self.assertTrue(any("状态文件不可写" in str(call) for call in log.call_args_list))

    # 等待期间停止或等待刚返回时停止，均不能在退出期间创建任务。
    def test_stop_during_wait_never_submits_job(self):
        for wait_result in (False, True):
            with self.subTest(wait_result=wait_result):
                self.manager.stopping.clear()

                def stop(seconds):
                    self.manager.stopping.set()
                    return wait_result

                with mock.patch.object(self.manager.stopping, "wait", side_effect=stop), \
                     mock.patch.object(self.manager, "begin") as begin:
                    self.manager.auto_update_loop()
                begin.assert_not_called()
        self.manager.stopping.clear()

    # 平台健康就绪后才启动自动调度，退出唤醒等待并回收调度线程。
    def test_scheduler_starts_after_ready_and_stops_on_close(self):
        entered = threading.Event()
        ready = False

        def healthy():
            nonlocal ready
            ready = True
            return True

        def loop():
            self.assertTrue(ready)
            entered.set()
            self.manager.stopping.wait()

        with mock.patch.object(self.manager, "launch"), \
             mock.patch.object(self.manager, "healthy", side_effect=healthy), \
             mock.patch.object(self.manager, "supervise"), \
             mock.patch.object(self.manager, "auto_update_loop", side_effect=loop), \
             mock.patch.object(self.manager, "stop_child"):
            self.manager.start()
            self.assertTrue(entered.wait(2))
            updater = self.manager.auto_updater
            self.assertTrue(updater.is_alive())
            self.manager.close()
            self.assertFalse(updater.is_alive())

    # 无有效来源或平台未就绪时不能启动自动调度。
    def test_scheduler_not_started_without_source_or_health(self):
        with mock.patch.object(self.manager, "launch"), mock.patch.object(self.manager, "stop_child"), \
             mock.patch.object(self.manager, "healthy", return_value=False), \
             mock.patch.object(self.manager, "auto_update_loop") as loop:
            with self.assertRaises(runtime.common.UpdateError):
                self.manager.start()
            loop.assert_not_called()
            self.assertIsNone(self.manager.auto_updater)
        self.manager.source_url = ""
        with mock.patch.object(self.manager, "launch"), mock.patch.object(self.manager, "stop_child"), \
             mock.patch.object(self.manager, "healthy", return_value=True), \
             mock.patch.object(self.manager, "supervise"), \
             mock.patch.object(self.manager, "auto_update_loop") as loop:
            self.manager.start()
            loop.assert_not_called()
            self.assertIsNone(self.manager.auto_updater)

    def run_local_tick(self, manager):
        real_begin = manager.begin

        def begin(operation):
            real_begin(operation)
            manager.worker.join(3)
            self.assertFalse(manager.worker.is_alive())

        with mock.patch.object(manager.stopping, "wait", side_effect=[False, True]), \
             mock.patch.object(manager, "begin", side_effect=begin):
            manager.auto_update_loop()

    # 自动轮次通过真实后台任务安装新候选并持久化；健康检查失败则保留旧版。
    def test_automatic_install_and_health_failure_restore_previous(self):
        manager = self.local_manager()
        data = support.manifest()
        candidate = manager.runtime_dir / "releases" / runtime.release_name(data["version"])
        candidate.mkdir()
        (candidate / "mskdsp-platform").write_bytes(b"candidate")
        original = copy.deepcopy(manager.current)
        with mock.patch.object(manager.local_source, "latest", return_value=(data, data["platforms"]["linux-amd64"])), \
             mock.patch.object(manager, "prepare", return_value=candidate), \
             mock.patch.object(manager, "launch") as launch, mock.patch.object(manager, "stop_child") as stop, \
             mock.patch.object(manager, "healthy", side_effect=[False, True, True]):
            self.run_local_tick(manager)
            self.assertEqual(manager.current, original)
            self.assertEqual(manager.status()["state"], "error")
            self.assertIn("已回滚", manager.status()["message"])
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(stop.call_count, 2)
            self.assertFalse(manager.busy)
            self.run_local_tick(manager)
            self.assertEqual(manager.status()["state"], "succeeded")
            self.assertEqual(manager.current["version"], data["version"])
            self.assertEqual(manager.current["previous"]["version"], original["version"])
            self.assertEqual(json.loads((manager.runtime_dir / "state.json").read_text())["version"], data["version"])
            self.assertEqual(launch.call_count, 3)
            self.assertEqual(stop.call_count, 3)
            self.assertFalse(manager.busy)

    # 无包或已是最新版时自动轮次保持现有服务，不准备或切换进程。
    def test_empty_and_current_candidates_never_restart_service(self):
        manager = self.local_manager()
        data = support.manifest()
        manager.current.update({field: data[field] for field in runtime.IDENTITY_FIELDS})
        for selected, state in ((None, "empty"), ((data, data["platforms"]["linux-amd64"]), "current")):
            with self.subTest(state=state), \
                 mock.patch.object(manager.local_source, "latest", return_value=selected), \
                 mock.patch.object(manager, "prepare") as prepare, mock.patch.object(manager, "stop_child") as stop:
                self.run_local_tick(manager)
                self.assertEqual(manager.status()["state"], state)
                self.assertFalse(manager.busy)
                prepare.assert_not_called()
                stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()

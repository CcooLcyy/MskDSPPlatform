"""真实进程集成测试数据目录隔离及空间边界。"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_modbus_api as api


class PlatformApiStorageTests(unittest.TestCase):
    # 模拟默认磁盘临时目录不可用：显式根仍可建立 SQLite、提交并完整清理。
    def test_explicit_root_avoids_default_disk_and_preserves_database_io(self):
        original = tempfile.TemporaryDirectory
        with original() as parent:
            root = Path(parent)
            def create(*args, **kwargs):
                if Path(kwargs.get("dir", "")) != root:
                    raise OSError("模拟磁盘临时目录阻塞")
                return original(*args, **kwargs)
            with mock.patch.dict(os.environ, {"MSKDSP_PLATFORM_TEST_TMPDIR": str(root)}), \
                 mock.patch.object(api.tempfile, "TemporaryDirectory", side_effect=create), \
                 mock.patch.object(shutil, "disk_usage", return_value=SimpleNamespace(free=64 * 1024 * 1024)):
                directory = api.temporary_api_directory()
                path = Path(directory.name)
                self.assertEqual(path.parent, root)
                with closing(sqlite3.connect(path / "test.sqlite3")) as db:
                    db.execute("CREATE TABLE values_test(value TEXT)")
                    db.execute("INSERT INTO values_test(value) VALUES ('已提交')")
                    db.commit()
                with closing(sqlite3.connect(path / "test.sqlite3")) as db:
                    self.assertEqual(db.execute("SELECT value FROM values_test").fetchone(), ("已提交",))
                directory.cleanup()
                self.assertFalse(path.exists())

    # 空间不足明确报错，不能悄悄回退到已经拥塞的默认磁盘。
    def test_low_capacity_is_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.dict(os.environ, {"MSKDSP_PLATFORM_TEST_TMPDIR": parent}), \
             mock.patch.object(shutil, "disk_usage", return_value=SimpleNamespace(free=32 * 1024 * 1024 - 1)), \
             mock.patch.object(api.tempfile, "TemporaryDirectory") as create:
            with self.assertRaisesRegex(RuntimeError, "32 MiB"):
                api.temporary_api_directory()
            create.assert_not_called()

    # 普通本地运行未配置隔离根时，仍使用原有平台默认临时目录。
    def test_unconfigured_local_run_keeps_default_temp_directory(self):
        original = tempfile.TemporaryDirectory
        with mock.patch.dict(os.environ, {"MSKDSP_PLATFORM_TEST_TMPDIR": ""}), \
             mock.patch.object(api.tempfile, "TemporaryDirectory", wraps=original) as create:
            directory = api.temporary_api_directory()
            try:
                self.assertNotIn("dir", create.call_args.kwargs)
                self.assertTrue(Path(directory.name).is_dir())
            finally:
                directory.cleanup()

    # 配置路径不存在时直接给出隔离目录错误，不创建默认磁盘目录。
    def test_missing_explicit_root_is_reported(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.dict(os.environ, {"MSKDSP_PLATFORM_TEST_TMPDIR": str(Path(parent) / "missing")}):
            with self.assertRaisesRegex(RuntimeError, "测试数据目录"):
                api.temporary_api_directory()

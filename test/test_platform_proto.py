"""平台构建必须固定引用共享协议，私有认证不能进入命令参数或磁盘。"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class PlatformProtoTests(unittest.TestCase):
    # 初始化显式使用 gitlink 固定提交，并将令牌限制在 Git 子进程环境。
    def test_submodule_initialization_keeps_credentials_out_of_arguments(self):
        spec = importlib.util.spec_from_file_location("init_platform_proto", ROOT / "script/init_platform_proto.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol = root / "platform-proto/platform/v1/software.proto"

            def run(command, **options):
                self.assertEqual(command, ["git", "submodule", "update", "--init", "--recursive", "--", "platform-proto"])
                self.assertNotIn("secret-value", " ".join(command))
                self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertNotIn("PLATFORM_PROTO_READ_TOKEN", options["env"])
                self.assertTrue(options["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
                protocol.parent.mkdir(parents=True)
                protocol.write_text('syntax = "proto3";', encoding="utf-8")

            with patch.dict(os.environ, {"PLATFORM_PROTO_READ_TOKEN": "secret-value"}), patch.object(module.subprocess, "run", side_effect=run):
                module.initialize(root)

    # Git 初始化返回成功但固定提交尚无协议文件时必须拒绝继续构建。
    def test_old_or_incomplete_protocol_checkout_is_rejected(self):
        spec = importlib.util.spec_from_file_location("init_platform_proto", ROOT / "script/init_platform_proto.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory, patch.object(module.subprocess, "run"):
            with self.assertRaisesRegex(RuntimeError, "缺少软件仓库协议"):
                module.initialize(Path(directory))


if __name__ == "__main__":
    unittest.main()

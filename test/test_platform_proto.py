"""平台构建固定引用公开共享协议，不向 GitHub 传递旧读取凭据。"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class PlatformProtoTests(unittest.TestCase):
    # 先同步旧工作副本地址，再按 gitlink 检出；旧读取令牌不能传给 GitHub。
    def test_submodule_initialization_keeps_credentials_out_of_arguments(self):
        spec = importlib.util.spec_from_file_location("init_platform_proto", ROOT / "script/init_platform_proto.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol = root / "platform-proto/platform/v1/software.proto"

            commands = []

            def run(command, **options):
                commands.append(command)
                self.assertNotIn("secret-value", " ".join(command))
                self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertNotIn("PLATFORM_PROTO_READ_TOKEN", options["env"])
                self.assertNotIn("GIT_CONFIG_VALUE_0", options["env"])
                self.assertTrue(options["check"])
                if command[2] == "sync":
                    return
                protocol.parent.mkdir(parents=True)
                protocol.write_text('syntax = "proto3";', encoding="utf-8")
                protocol.with_name("templates.proto").write_text('syntax = "proto3";', encoding="utf-8")

            with patch.dict(os.environ, {"PLATFORM_PROTO_READ_TOKEN": "secret-value"}, clear=True), patch.object(module.subprocess, "run", side_effect=run):
                module.initialize(root)
            self.assertEqual(commands, [
                ["git", "submodule", "sync", "--", "platform-proto"],
                ["git", "submodule", "update", "--init", "--recursive", "--", "platform-proto"],
            ])

    # 公开仓库不配置令牌也必须能初始化，旧令牌不再是 CI 前置条件。
    def test_public_submodule_initialization_without_token(self):
        spec = importlib.util.spec_from_file_location("init_platform_proto", ROOT / "script/init_platform_proto.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol = root / "platform-proto/platform/v1/software.proto"
            protocol.parent.mkdir(parents=True)
            protocol.write_text('syntax = "proto3";', encoding="utf-8")
            protocol.with_name("templates.proto").write_text('syntax = "proto3";', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(module.subprocess, "run") as run:
                module.initialize(root)
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertNotIn("GIT_CONFIG_COUNT", call.kwargs["env"])

    # Git 初始化返回成功但固定提交尚无协议文件时必须拒绝继续构建。
    def test_old_or_incomplete_protocol_checkout_is_rejected(self):
        spec = importlib.util.spec_from_file_location("init_platform_proto", ROOT / "script/init_platform_proto.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory, patch.object(module.subprocess, "run"):
            with self.assertRaisesRegex(RuntimeError, "缺少软件仓库协议"):
                module.initialize(Path(directory))

    def test_software_only_checkout_rejects_missing_template_catalog_protocol(self):
        spec = importlib.util.spec_from_file_location("init_platform_proto", ROOT / "script/init_platform_proto.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory, patch.object(module.subprocess, "run"):
            root = Path(directory)
            software = root / "platform-proto/platform/v1/software.proto"
            software.parent.mkdir(parents=True)
            software.write_text('syntax = "proto3";', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "点表目录协议"):
                module.initialize(root)


if __name__ == "__main__":
    unittest.main()

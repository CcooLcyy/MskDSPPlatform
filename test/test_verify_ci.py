"""验证平台检查作业的持久缓存、挂载边界和原有失败门禁。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["VERIFY_COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, args, {key: os.environ.get(key) for key in
        ("CARGO_HOME", "CARGO_TARGET_DIR", "TMPDIR", "MSKDSP_PLATFORM_TEST_TMPDIR")}]) + "\n")
if name == "cargo":
    for key in ("CARGO_HOME", "CARGO_TARGET_DIR"):
        marker = Path(os.environ[key]) / "cache-sentinel"
        if os.environ.get("EXPECT_CACHE_SENTINEL") and marker.read_text() != "跨作业复用":
            sys.exit("缓存种子未复用")
    if args[0] == "build" and os.environ.get("OMIT_PLATFORM_BINARY") != "1":
        binary = Path(os.environ["CARGO_TARGET_DIR"]) / "debug/mskdsp-platform"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("平台程序种子")
        binary.chmod(0o755)
    if args[0] == os.environ.get("FAIL_CARGO_STAGE"):
        print("模拟 Cargo 阶段失败")
        sys.exit(27)
'''


class VerifyConfigurationTests(unittest.TestCase):
    # 检查作业显式禁用 ZIP，串行访问固定工具链缓存且仅归档日志。
    def test_verify_uses_direct_cache_and_serializes_all_verify_jobs(self):
        config = (ROOT / ".gitlab/ci/ci.yml").read_text(encoding="utf-8")
        verify = config.split("\nverify:\n", 1)[1].split("\nmetadata:\n", 1)[0]
        for token in ("CARGO_HOME: /ci-cache/platform/rust-1.94.1/cargo",
                      "CARGO_TARGET_DIR: /ci-cache/platform/rust-1.94.1/target",
                      "PLATFORM_CACHE_MOUNT: /ci-cache", "cache: []",
                      "resource_group: platform-verify-cache", "bash script/verify_ci.sh",
                      "paths: [package/logs/]"):
            self.assertIn(token, verify)
        self.assertNotIn("target/debug", verify)
        self.assertNotIn("policy: pull-push", verify)
        self.assertNotIn(".cargo/registry/", verify)


@unittest.skipUnless(os.name == "posix", "真实 Bash 与挂载契约在 Linux/WSL 执行")
class VerifyScriptTests(unittest.TestCase):
    def workspace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "script").mkdir()
        script = ROOT / "script/verify_ci.sh"
        if script.exists():
            shutil.copyfile(script, root / "script/verify_ci.sh")
        tools = root / "bin"
        tools.mkdir()
        for name in ("cargo", "python3", "node"):
            executable = tools / name
            executable.write_text("#!" + sys.executable + "\n" + MOCK, encoding="utf-8")
            executable.chmod(0o755)
        environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                           VERIFY_COMMAND_LOG=str(root / "commands.jsonl"))
        for key in ("CARGO_HOME", "CARGO_TARGET_DIR", "PLATFORM_CACHE_MOUNT", "FAIL_CARGO_STAGE",
                    "EXPECT_CACHE_SENTINEL", "OMIT_PLATFORM_BINARY"):
            environment.pop(key, None)
        return root, environment

    def cache(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ("cargo", "target"):
            (root / name).mkdir()
            (root / name / "cache-sentinel").write_text("跨作业复用", encoding="utf-8")
        return root

    def run_script(self, root, environment):
        return subprocess.run(["bash", "script/verify_ci.sh"], cwd=root, env=environment,
                              capture_output=True, text=True, timeout=20)

    def commands(self, root):
        path = root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # 不同作业工作区复用外部缓存，原检查顺序与 tmpfs 测试设置保持一致。
    def test_separate_workspaces_reuse_cargo_and_target_without_zip(self):
        cache = self.cache()
        for _ in range(2):
            root, environment = self.workspace()
            environment.update(CARGO_HOME=str(cache / "cargo"), CARGO_TARGET_DIR=str(cache / "target"),
                               PLATFORM_CACHE_MOUNT="/", EXPECT_CACHE_SENTINEL="1")
            result = self.run_script(root, environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            commands = self.commands(root)
            self.assertEqual([(name, args) for name, args, _ in commands], [
                ("python3", ["-B", "script/init_platform_proto.py"]),
                ("cargo", ["fmt", "--all", "--", "--check"]),
                ("cargo", ["build", "--locked"]), ("node", ["--version"]),
                ("cargo", ["test", "--locked", "--all-targets"]),
                ("python3", ["-B", "-m", "unittest", "discover", "-s", "test", "-p", "test_*.py", "-v"]),
            ])
            for _, _, exported in commands:
                self.assertEqual(exported["CARGO_HOME"], str(cache / "cargo"))
                self.assertEqual(exported["CARGO_TARGET_DIR"], str(cache / "target"))
                self.assertEqual(exported["TMPDIR"], str(root / ".ci-tmp"))
            self.assertEqual(commands[-1][2]["MSKDSP_PLATFORM_TEST_TMPDIR"], "/dev/shm")
            self.assertTrue((root / "package/logs/cargo-build.log").is_file())
            self.assertFalse((root / "target").exists())
            self.assertIn(str(cache / "target"), result.stdout)
            self.assertIn("缓存", result.stdout)
        self.assertTrue((cache / "target/debug/mskdsp-platform").is_file())

    # 原本地调用无需持久挂载，默认 target 与 Cargo 目录仍在项目内。
    def test_local_defaults_preserve_project_cache(self):
        root, environment = self.workspace()
        result = self.run_script(root, environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exported = self.commands(root)[0][2]
        self.assertEqual(exported["CARGO_HOME"], str(root / ".cargo"))
        self.assertEqual(exported["CARGO_TARGET_DIR"], str(root / "target"))

    # 无真实挂载、相对路径或工作区内路径必须在执行 Cargo 前明确失败。
    def test_invalid_cache_boundaries_fail_before_any_tools(self):
        cache = self.cache()
        scenarios = (
            ({"PLATFORM_CACHE_MOUNT": str(cache)}, "挂载"),
            ({"CARGO_TARGET_DIR": "target", "PLATFORM_CACHE_MOUNT": "/"}, "绝对路径"),
            ({"CARGO_HOME": ".cargo", "PLATFORM_CACHE_MOUNT": "/"}, "绝对路径"),
            ({"PLATFORM_CACHE_MOUNT": "cache"}, "绝对路径"),
        )
        for overrides, label in scenarios:
            with self.subTest(overrides=overrides):
                root, environment = self.workspace()
                environment.update(CARGO_HOME=str(cache / "cargo"), CARGO_TARGET_DIR=str(cache / "target"))
                environment.update(overrides)
                result = self.run_script(root, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(label, result.stdout + result.stderr)
                self.assertEqual(self.commands(root), [])
        for key in ("CARGO_HOME", "CARGO_TARGET_DIR"):
            with self.subTest(workspace_path=key):
                root, environment = self.workspace()
                environment.update(CARGO_HOME=str(cache / "cargo"), CARGO_TARGET_DIR=str(cache / "target"),
                                   PLATFORM_CACHE_MOUNT="/")
                environment[key] = str(root / "nested-cache")
                result = self.run_script(root, environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("工作区", result.stdout + result.stderr)
                self.assertEqual(self.commands(root), [])

    # 缓存根中的符号链接不能绕过真实挂载边界检查。
    def test_symlink_cannot_escape_mount(self):
        if not os.path.ismount("/dev/shm"):
            self.skipTest("当前环境缺少 /dev/shm 真实挂载")
        temporary = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(temporary.cleanup)
        cache = self.cache()
        link = Path(temporary.name) / "escape"
        link.symlink_to(cache, target_is_directory=True)
        root, environment = self.workspace()
        environment.update(CARGO_HOME=str(link / "cargo"), CARGO_TARGET_DIR=str(link / "target"),
                           PLATFORM_CACHE_MOUNT="/dev/shm")
        result = self.run_script(root, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("挂载", result.stdout + result.stderr)
        self.assertEqual(self.commands(root), [])

    # 管道中的编译/测试失败必须保留退出码并阻止后续测试。
    def test_cargo_failure_stops_subsequent_checks(self):
        for phase in ("build", "test"):
            with self.subTest(phase=phase):
                root, environment = self.workspace()
                environment["FAIL_CARGO_STAGE"] = phase
                result = self.run_script(root, environment)
                self.assertEqual(result.returncode, 27, result.stdout + result.stderr)
                self.assertFalse(any(name == "python3" and "unittest" in args
                                     for name, args, _ in self.commands(root)))

    # 构建未生成实际程序时拒绝继续，避免真实平台测试因路径不存在而静默跳过。
    def test_missing_built_binary_blocks_python_tests(self):
        root, environment = self.workspace()
        environment["OMIT_PLATFORM_BINARY"] = "1"
        result = self.run_script(root, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("程序", result.stdout + result.stderr)
        self.assertFalse(any(name == "python3" and "unittest" in args
                             for name, args, _ in self.commands(root)))


if __name__ == "__main__":
    unittest.main()

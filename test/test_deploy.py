"""部署脚本在宿主机工作目录内保存配置和运行文件。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "部署行为测试需要 POSIX shell")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.deploy = self.bundle / "deploy"
        self.deploy.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "deploy"
        shutil.copy(source / "run.sh", self.deploy / "run.sh")
        (self.deploy / "config.toml").write_text("[server]\n", encoding="utf-8")
        (self.deploy / "updater.json").write_text('{"manifest_url":"https://example.com/ci/latest.json"}', encoding="utf-8")
        (self.bundle / ".env").write_text("MSKDSP_PLATFORM_IMAGE=example/platform:test\n", encoding="utf-8")
        self.work = self.root / "platform work"
        self.commands = self.root / "commands.jsonl"
        mock = self.root / "bin"
        mock.mkdir()
        docker = mock / "docker"
        docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ['TEST_DOCKER_LOG'],'a') as out:\n out.write(json.dumps(sys.argv[1:])+'\\n')\n", encoding="utf-8")
        docker.chmod(0o755)
        self.env = dict(os.environ, MSKDSP_WORK_DIR=str(self.work), TEST_DOCKER_LOG=str(self.commands),
                        PATH=str(mock) + os.pathsep + os.environ["PATH"])

    def run_script(self):
        return subprocess.run(["sh", str(self.deploy / "run.sh")], env=self.env, text=True, capture_output=True)

    # 用真实文件与 shell 验证所有运行挂载、工作目录与显式环境，且不创建命名卷。
    def test_work_directory_contains_config_data_logs_and_temporary_files(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative in ["deploy/config.toml", "deploy/updater.json", "runtime/data", "runtime/logs", "runtime/tmp"]:
            self.assertTrue((self.work / relative).exists(), relative)
        commands = [json.loads(line) for line in self.commands.read_text().splitlines()]
        self.assertFalse(any(command[:2] == ["volume", "create"] for command in commands))
        run = next(command for command in commands if "--name" in command)
        self.assertEqual(run[-1], "example/platform:test")
        self.assertIn(f"type=bind,source={self.work}/runtime,target=/var/lib/mskdsp-platform", run)
        self.assertIn(f"type=bind,source={self.work}/runtime/tmp,target=/tmp", run)
        self.assertIn("MSKDSP_PLATFORM_DATA_DIR=/var/lib/mskdsp-platform/data", run)
        self.assertIn("TMPDIR=/var/lib/mskdsp-platform/tmp", run)
        self.assertIn("/var/lib/mskdsp-platform", run)

    # 再部署不得覆盖管理员配置、更新源或既有业务数据。
    def test_preserves_existing_configuration_and_data(self):
        (self.work / "deploy").mkdir(parents=True)
        (self.work / "deploy/config.toml").write_text("existing config", encoding="utf-8")
        (self.work / "deploy/updater.json").write_text("existing source", encoding="utf-8")
        (self.work / "runtime/data").mkdir(parents=True)
        data = self.work / "runtime/data/record.json"
        data.write_text("existing data", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "deploy/config.toml").read_text(), "existing config")
        self.assertEqual((self.work / "deploy/updater.json").read_text(), "existing source")
        self.assertEqual(data.read_text(), "existing data")

    # 相对路径与宿主根目录不能被误用为平台工作目录。
    def test_rejects_relative_or_root_work_directory_before_docker(self):
        for value in ["relative/platform", "/", "/tmp/.."]:
            with self.subTest(value=value):
                self.env["MSKDSP_WORK_DIR"] = value
                result = self.run_script()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.commands.exists())


if __name__ == "__main__":
    unittest.main()

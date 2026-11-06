"""正式发布在真实临时 Git 历史上的标签与来源门禁回归，仅由 CI 执行。"""
from pathlib import Path
import base64
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import release


class StableSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "source"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "发布回归")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgsign", "false")
        self.git("remote", "add", "origin", str(self.repo))
        self.git("commit", "--allow-empty", "-m", "initial")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.0.1")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True, encoding="utf-8", stderr=subprocess.PIPE).strip()

    def verify(self):
        return release.verify_remote_stable_source("v0.0.1", "0.0.1", self.sha, repo=self.repo)

    # 轻量与附注标签均解引用到冻结提交，main 前进不阻止旧提交正式发布。
    def test_lightweight_annotated_and_advanced_main(self):
        self.verify()
        self.git("tag", "-d", "v0.0.1")
        self.git("tag", "-a", "v0.0.1", "-m", "release", self.sha)
        self.git("commit", "--allow-empty", "-m", "next")
        self.verify()

    # main 不包含提交时，匹配版本维护分支的真实祖先关系仍可放行。
    def test_matching_release_history(self):
        self.git("checkout", "-b", "release/0.0")
        self.git("commit", "--allow-empty", "-m", "patch")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("tag", "-f", "v0.0.1")
        self.git("commit", "--allow-empty", "-m", "later-patch")
        self.verify()

    # 错误维护线及仅属于 Beta 的提交均不能成为正式发布来源。
    def test_wrong_release_or_beta_history_is_rejected(self):
        for branch in ("release/0.1", "release/0.0.1", "release/00.0", "beta/0.0.1"):
            with self.subTest(branch=branch):
                self.git("checkout", "-B", branch, "main")
                self.git("commit", "--allow-empty", "-m", branch)
                self.sha = self.git("rev-parse", "HEAD")
                self.git("tag", "-f", "v0.0.1")
                with self.assertRaisesRegex(ValueError, "历史"):
                    self.verify()

    # 即使维护分支名称匹配，实际历史不包含冻结提交也必须拒绝。
    def test_matching_name_without_ancestry_is_rejected(self):
        self.git("branch", "release/0.0")
        self.git("checkout", "-b", "feature")
        self.git("commit", "--allow-empty", "-m", "unrelated")
        self.sha = self.git("rev-parse", "HEAD")
        self.git("tag", "-f", "v0.0.1")
        with self.assertRaisesRegex(ValueError, "历史"):
            self.verify()

    # 删除或移动远端标签都不能复用已经冻结的发布身份。
    def test_missing_or_moved_tag_is_rejected(self):
        self.git("tag", "-d", "v0.0.1")
        with self.assertRaisesRegex(ValueError, "标签"):
            self.verify()
        self.git("commit", "--allow-empty", "-m", "moved")
        self.git("tag", "v0.0.1")
        with self.assertRaisesRegex(ValueError, "标签"):
            self.verify()

    # 远端查询期间分支发生变化时拒绝使用两次观察拼接的来源证据。
    def test_reference_change_during_verification_is_rejected(self):
        original = release.git
        calls = 0
        def changing(*args, **kwargs):
            nonlocal calls
            if args and args[0] == "ls-remote":
                calls += 1
                if calls == 2:
                    self.git("commit", "--allow-empty", "-m", "concurrent")
            return original(*args, **kwargs)
        with patch.object(release, "git", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "变化"):
                self.verify()


class WorkflowTests(unittest.TestCase):
    # 临时取证仓库使用子进程环境继承短期凭据，原始令牌不进入命令参数或持久配置。
    def test_git_authentication_is_injected_only_into_subprocess_environment(self):
        with patch.dict(release.os.environ, {"PLATFORM_SOURCE_READ_TOKEN": "test-token"}, clear=True), \
                patch.object(release.subprocess, "check_output", return_value="ok") as command:
            self.assertEqual(release.git("ls-remote", "origin", cwd="temporary-bare"), "ok")
            self.assertEqual(command.call_args.args[0], ["git", "ls-remote", "origin"])
            environment = command.call_args.kwargs["env"]
            self.assertNotIn("PLATFORM_SOURCE_READ_TOKEN", environment)
            self.assertEqual(environment["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(environment["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraHeader")
            self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " +
                             base64.b64encode(b"x-access-token:test-token").decode("ascii"))

    # 可复用入口也仅允许 Stable，且各外部发布步骤重新校验冻结来源。
    def test_beta_entry_removed_and_stable_publications_are_gated(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / ".github/workflows/beta.yml").exists())
        workflow = (root / ".github/workflows/docker-release.yml").read_text(encoding="utf-8")
        self.assertIn("if: inputs.channel == 'stable'", workflow)
        self.assertIn("PLATFORM_SOURCE_READ_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertEqual(workflow.count("python script/release.py verify-source --metadata package/metadata.json"), 3)
        self.assertNotIn("--prerelease", workflow)

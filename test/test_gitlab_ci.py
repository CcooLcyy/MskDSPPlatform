"""GitLab 构建身份及发布前分支检查，隔离网络和真实 CI 凭据。"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import gitlab_ci

SHA = "0123456789abcdef0123456789abcdef01234567"
ENV = {
    "CI_COMMIT_SHA": SHA, "CI_COMMIT_BRANCH": "main", "CI_DEFAULT_BRANCH": "main",
    "CI_PIPELINE_IID": "42", "CI_JOB_ID": "101", "CI_PIPELINE_SOURCE": "push",
    "CI_PROJECT_URL": "http://gitlab.test/mskdsp/platform",
}


class GitlabMetadataTests(unittest.TestCase):
    def metadata(self, changes=None, **options):
        env = ENV | (changes or {})
        with patch.dict(os.environ, env, clear=True), \
                patch.object(gitlab_ci, "git", return_value=SHA), \
                patch.object(gitlab_ci.release, "read_version", return_value="0.0.1"), \
                patch.object(gitlab_ci, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
            return gitlab_ci.build_ci_metadata(**options)

    def test_main_identity_uses_gitlab_source_and_explicit_image_default(self):
        data = self.metadata()
        self.assertEqual(data["version"], "0.0.1-ci.20261016t080000.42.101+sha.0123456")
        self.assertEqual(data["source"], ENV["CI_PROJECT_URL"])
        self.assertEqual(data["image_repository"], "ghcr.io/ccoolcyy/mskdsp-platform")
        self.assertEqual(data["ref"], "main")
        self.assertNotIn("update_manifest_url", data)

    def test_job_retry_produces_unique_version_even_at_same_time(self):
        first = self.metadata()
        retry = self.metadata({"CI_JOB_ID": "102"})
        self.assertNotEqual(first["version"], retry["version"])

    def test_source_gate_rejects_merge_requests_tags_and_other_branches(self):
        for changes in ({"CI_PIPELINE_SOURCE": "merge_request_event"},
                        {"CI_COMMIT_BRANCH": "feature/test"},
                        {"CI_COMMIT_TAG": "v0.0.1"},
                        {"CI_DEFAULT_BRANCH": "develop"},
                        {"CI_PIPELINE_SOURCE": "schedule"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.metadata(changes)

    def test_rejects_checkout_mismatch_and_malformed_ci_identity(self):
        for changes in ({"CI_COMMIT_SHA": "f" * 40}, {"CI_JOB_ID": "123\nEVIL=yes"},
                        {"CI_PROJECT_URL": "http://user:secret@gitlab.test/project"},
                        {"CI_PIPELINE_IID": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.metadata(changes)

    def test_image_repository_overrides_preserve_frozen_identity(self):
        data = self.metadata(image_repository="registry.test:5000/team/platform")
        self.assertTrue(data["image"].startswith("registry.test:5000/team/platform:"))
        self.assertEqual(self.metadata({"IMAGE_REPOSITORY": "registry.test/team/image"})["image_repository"],
                         "registry.test/team/image")
        with self.assertRaises(ValueError):
            self.metadata(image_repository="registry.test/team/image:mutable")

    def test_cli_writes_json_and_dotenv_for_downstream_jobs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                gitlab_ci, "build_ci_metadata", return_value=self.metadata()):
            root = Path(tmp)
            gitlab_ci.main(["metadata", "--output", str(root / "package/metadata.json"),
                            "--dotenv", str(root / "package/build.env")])
            data = json.loads((root / "package/metadata.json").read_text(encoding="utf-8"))
            env = (root / "package/build.env").read_text(encoding="utf-8")
            self.assertIn(f"PLATFORM_VERSION={data['version']}\n", env)
            self.assertIn(f"PLATFORM_COMMIT={SHA}\n", env)


class PublishHeadTests(unittest.TestCase):
    metadata = {"commit": SHA, "ref": "main", "channel": "ci", "source": ENV["CI_PROJECT_URL"]}

    def test_fetches_before_comparing_frozen_commit(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                gitlab_ci, "git", side_effect=["gitlab\norigin", "", SHA]) as git:
            gitlab_ci.publish_head_check(self.metadata)
            self.assertEqual(git.call_args_list[1].args,
                             ("fetch", "--no-tags", "--depth=1", "gitlab", "+refs/heads/main:refs/remotes/gitlab/main"))

    def test_old_pipeline_cannot_publish_latest(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                gitlab_ci, "git", side_effect=["origin", "", "f" * 40]):
            with self.assertRaisesRegex(ValueError, "旧构建"):
                gitlab_ci.publish_head_check(self.metadata)

    def test_internal_clone_url_credentials_never_enter_command_or_failure_output(self):
        url = "http://gitlab-ci-token:super-secret@internal.test:8929/mskdsp/platform.git"
        env = ENV | {"CI_REPOSITORY_URL": url}
        calls = []
        def failed(command, **kwargs):
            calls.append((command, kwargs))
            raise subprocess.CalledProcessError(1, command, stderr="super-secret rejected " + url)
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(subprocess, "run", side_effect=failed), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(ValueError) as error:
                gitlab_ci.publish_head_check(self.metadata)
        self.assertNotIn("super-secret", str(error.exception) + output.getvalue())
        self.assertNotIn("super-secret", repr(calls[0][0]))
        self.assertIn("http://internal.test:8929/mskdsp/platform.git", calls[0][0])
        self.assertIn("GIT_CONFIG_VALUE_0", calls[0][1]["env"])

    def test_current_head_uses_origin_when_gitlab_remote_absent(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                gitlab_ci, "git", side_effect=["origin", "", SHA]) as git:
            gitlab_ci.publish_head_check(self.metadata)
            self.assertEqual(git.call_args_list[1].args[3], "origin")

    def test_server_url_can_route_project_to_internal_gitlab(self):
        env = {"CI_SERVER_URL": "http://internal.test:8929", "CI_JOB_TOKEN": "secret-token"}
        with patch.dict(os.environ, env, clear=True), patch.object(
                gitlab_ci, "git", side_effect=["", SHA]) as git:
            gitlab_ci.publish_head_check(self.metadata)
            fetch = git.call_args_list[0]
            self.assertEqual(fetch.args[3], "http://internal.test:8929/mskdsp/platform.git")
            self.assertIn("GIT_CONFIG_VALUE_0", fetch.kwargs["env"])
            self.assertNotIn("secret-token", repr(fetch.args))

    def test_rejects_non_ci_metadata_without_network(self):
        with patch.object(gitlab_ci, "git") as git:
            with self.assertRaises(ValueError):
                gitlab_ci.publish_head_check(self.metadata | {"channel": "stable"})
            git.assert_not_called()

    def test_shallow_fetch_still_rejects_old_head_and_accepts_latest(self):
        # 使用本地真实 Git 传输验证浅历史，不依赖网络或服务器凭据。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, checkout = root / "source", root / "checkout"
            source.mkdir()
            def run(*args, cwd=source):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                                      text=True, encoding="utf-8").stdout.strip()
            run("init", "--initial-branch=main")
            run("config", "user.name", "CI test")
            run("config", "user.email", "ci-test@example.invalid")
            (source / "file").write_text("first", encoding="utf-8")
            run("add", "file")
            run("commit", "-m", "first")
            old = run("rev-parse", "HEAD")
            run("clone", "--depth=1", source.as_uri(), str(checkout), cwd=root)
            (source / "file").write_text("second", encoding="utf-8")
            run("commit", "-am", "second")
            latest = run("rev-parse", "HEAD")
            original_git = gitlab_ci.git
            def local_git(*args, **kwargs):
                # 不改全局 cwd，避免影响并行测试或其他任务。
                return original_git("-C", str(checkout), *args, **kwargs)
            with patch.dict(os.environ, {"CI_REPOSITORY_URL": "", "CI_SERVER_URL": ""}), \
                    patch.object(gitlab_ci, "git", side_effect=local_git):
                with self.assertRaisesRegex(ValueError, "旧构建"):
                    gitlab_ci.publish_head_check(self.metadata | {"commit": old})
                gitlab_ci.publish_head_check(self.metadata | {"commit": latest})
            self.assertEqual(run("rev-list", "--count", "refs/remotes/origin/main", cwd=checkout), "1")


class HeadCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.meta = Path(self.temporary.name) / "metadata.json"
        self.meta.write_text(json.dumps(PublishHeadTests.metadata), encoding="utf-8")
        self.argv = ["publish-head-check", "--metadata", str(self.meta)]

    def test_cli_checks_frozen_metadata_against_requested_remote(self):
        with patch.object(gitlab_ci, "publish_head_check") as check:
            gitlab_ci.main(self.argv + ["--remote", "gitlab"])
        check.assert_called_once_with(PublishHeadTests.metadata, "gitlab")

    def test_cli_hides_subprocess_credentials_and_stderr(self):
        failure = subprocess.CalledProcessError(1, ["git", "secret-argument"], stderr="secret-stderr")
        output = io.StringIO()
        with patch.object(gitlab_ci, "publish_head_check", side_effect=failure), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                gitlab_ci.cli(self.argv)
        self.assertTrue(error.exception.code.startswith("GitLab CI 校验失败"))
        self.assertNotIn("secret", str(error.exception) + output.getvalue())


if __name__ == "__main__":
    unittest.main()

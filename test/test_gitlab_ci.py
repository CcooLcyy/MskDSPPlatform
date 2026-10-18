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

    def test_image_override_and_https_channel_update_url(self):
        data = self.metadata(image_repository="registry.test:5000/team/platform",
                             update_manifest_url="https://update.test/platform/ci/latest.json")
        self.assertTrue(data["image"].startswith("registry.test:5000/team/platform:"))
        self.assertEqual(data["update_manifest_url"], "https://update.test/platform/ci/latest.json")
        self.assertEqual(self.metadata({"IMAGE_REPOSITORY": "registry.test/team/image"})["image_repository"],
                         "registry.test/team/image")
        for url in ("http://update.test/ci/latest.json", "https://update.test/beta/latest.json"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.metadata(update_manifest_url=url)
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
                             ("fetch", "--no-tags", "gitlab", "+refs/heads/main:refs/remotes/gitlab/main"))

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
            self.assertEqual(git.call_args_list[1].args[2], "origin")

    def test_server_url_can_route_project_to_internal_gitlab(self):
        env = {"CI_SERVER_URL": "http://internal.test:8929", "CI_JOB_TOKEN": "secret-token"}
        with patch.dict(os.environ, env, clear=True), patch.object(
                gitlab_ci, "git", side_effect=["", SHA]) as git:
            gitlab_ci.publish_head_check(self.metadata)
            fetch = git.call_args_list[0]
            self.assertEqual(fetch.args[2], "http://internal.test:8929/mskdsp/platform.git")
            self.assertIn("GIT_CONFIG_VALUE_0", fetch.kwargs["env"])
            self.assertNotIn("secret-token", repr(fetch.args))

    def test_rejects_non_ci_metadata_without_network(self):
        with patch.object(gitlab_ci, "git") as git:
            with self.assertRaises(ValueError):
                gitlab_ci.publish_head_check(self.metadata | {"channel": "stable"})
            git.assert_not_called()


class PublishCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.meta = Path(self.temporary.name) / "metadata.json"
        self.meta.write_text(json.dumps(PublishHeadTests.metadata), encoding="utf-8")
        self.argv = ["publish-r2", "--metadata", str(self.meta), "--output-dir", "r2",
                     "--bucket", "mskdsp-update", "--public-base-url", "https://updates.test"]

    def test_checks_head_before_publish_and_again_before_latest(self):
        events = []
        def publish(**kwargs):
            events.append("assets")
            kwargs["before_latest"]()
            events.append("latest")
        with patch.object(gitlab_ci, "publish_head_check", side_effect=lambda data: events.append("head")), \
                patch.object(gitlab_ci.publish_r2, "publish", side_effect=publish):
            gitlab_ci.main(self.argv)
        self.assertEqual(events, ["head", "assets", "head", "latest"])

    def test_old_head_stops_before_publisher(self):
        with patch.object(gitlab_ci, "publish_head_check", side_effect=gitlab_ci.CIError("旧构建")), \
                patch.object(gitlab_ci.publish_r2, "publish") as publish:
            with self.assertRaisesRegex(ValueError, "旧构建"):
                gitlab_ci.main(self.argv)
            publish.assert_not_called()

    def test_cli_hides_subprocess_credentials_and_stderr(self):
        failure = subprocess.CalledProcessError(1, ["aws", "secret-argument"], stderr="secret-stderr")
        output = io.StringIO()
        with patch.object(gitlab_ci, "publish_head_check"), \
                patch.object(gitlab_ci.publish_r2, "publish", side_effect=failure), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as error:
                gitlab_ci.cli(self.argv)
        self.assertEqual(error.exception.code.startswith("GitLab CI 校验失败"), True)
        self.assertNotIn("secret", str(error.exception) + output.getvalue())


if __name__ == "__main__":
    unittest.main()

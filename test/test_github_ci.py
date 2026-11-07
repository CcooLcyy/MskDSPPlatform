"""验证 GitHub CI 身份及上传门禁，使用固定的工作流环境和远端响应。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import github_ci

SHA = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "CcooLcyy/MskDSPPlatform"
SOURCE = f"https://github.com/{REPOSITORY}"
MAIN_REF = "refs/heads/main"
ENV = {
    "GITHUB_EVENT_NAME": "push", "GITHUB_REF": MAIN_REF, "GITHUB_SHA": SHA,
    "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_NUMBER": "42", "GITHUB_RUN_ATTEMPT": "2",
}
METADATA = {"channel": "ci", "ref": "main", "commit": SHA, "source": SOURCE}


class GithubCiTests(unittest.TestCase):
    def test_manual_main_writes_frozen_metadata_and_workflow_outputs(self):
        env = ENV | {"GITHUB_EVENT_NAME": "workflow_dispatch"}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, env, clear=True), \
                patch.object(github_ci, "git", return_value=SHA), \
                patch.object(github_ci.release, "read_version", return_value="0.0.1"), \
                patch.object(github_ci, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 7, 1, 2, 3, tzinfo=timezone.utc)
            metadata = Path(temporary) / "package/metadata.json"
            outputs = Path(temporary) / "outputs"
            github_ci.main(["metadata", "--output", str(metadata), "--github-output", str(outputs)])
            data = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual({key: data[key] for key in METADATA}, METADATA)
            self.assertEqual(data["build_time"], "2026-11-07T01:02:03Z")
            self.assertEqual(data["version"], "0.0.1-ci.20261107t090203.42.2+sha.0123456")
            self.assertEqual(data["image"], "ghcr.io/ccoolcyy/mskdsp-platform:0.0.1-ci.20261107t090203.42.2-sha.0123456")
            self.assertNotIn("update_manifest_url", data)
            workflow_outputs = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
            self.assertEqual(workflow_outputs, data)

    def test_metadata_rejects_pr_other_branches_tags_and_checkout_mismatch(self):
        for changes in ({"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_REF": "refs/heads/topic"},
                        {"GITHUB_REF": "refs/tags/v0.0.1"}, {"GITHUB_SHA": "f" * 40},
                        {"GITHUB_RUN_ATTEMPT": "0"}, {"GITHUB_RUN_NUMBER": "1\nuntrusted=yes"}):
            with self.subTest(changes=changes), patch.dict(os.environ, ENV | changes, clear=True), \
                    patch.object(github_ci, "git", return_value=SHA):
                with self.assertRaises(github_ci.CIError):
                    github_ci.build_ci_metadata()

    def test_upload_queries_fresh_github_main_and_rejects_old_build(self):
        with patch.dict(os.environ, ENV, clear=True):
            for remote_sha in (SHA, "f" * 40):
                with self.subTest(remote_sha=remote_sha), patch.object(
                        github_ci, "git", side_effect=[SHA, SOURCE + ".git", f"{remote_sha}\t{MAIN_REF}"]) as git:
                    if remote_sha == SHA:
                        github_ci.verify_head(METADATA)
                    else:
                        with self.assertRaisesRegex(github_ci.CIError, "旧构建"):
                            github_ci.verify_head(METADATA)
                    git.assert_called_with("ls-remote", "--exit-code", "origin", MAIN_REF)

    def test_upload_rejects_unrelated_remote_or_modified_identity_before_network(self):
        with patch.dict(os.environ, ENV, clear=True):
            with patch.object(github_ci, "git", side_effect=[SHA, "https://github.com/other/platform.git"]) as git:
                with self.assertRaisesRegex(github_ci.CIError, "origin"):
                    github_ci.verify_head(METADATA)
                git.assert_called_with("remote", "get-url", "origin")
            for changed in ({"source": "https://github.com/other/platform"}, {"commit": "f" * 40},
                            {"channel": "stable"}, {"ref": "topic"}):
                with self.subTest(changed=changed), patch.object(github_ci, "git", return_value=SHA) as git:
                    with self.assertRaisesRegex(github_ci.CIError, "冻结身份"):
                        github_ci.verify_head(METADATA | changed)
                    git.assert_called_once_with("rev-parse", "HEAD")


if __name__ == "__main__":
    unittest.main()

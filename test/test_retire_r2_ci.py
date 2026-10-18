"""通过隔离假存储验证旧 CI 快照、删除范围和失败记录，不连接真实 R2。"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import retire_r2_ci as retirement


class MemoryStore:
    """模拟独立对象存储，保留实际读写行为和调用记录。"""
    def __init__(self):
        self.payloads = {retirement.IMAGE_KEY: b"image backup", retirement.BINARY_KEY: b"binary backup"}
        entry = {
            "image": "ghcr.io/ccoolcyy/mskdsp-platform:" + retirement.TAG,
            "image_id": "sha256:" + "a" * 64,
        }
        for marker, key in (("", retirement.IMAGE_KEY), ("binary_", retirement.BINARY_KEY)):
            content = self.payloads[key]
            entry.update({marker + "url": retirement.PUBLIC_BASE + "/" + key,
                          marker + "size": len(content), marker + "sha256": hashlib.sha256(content).hexdigest()})
        manifest = dict(schema_version=1, project="mskdsp-platform", channel="ci", base_version="0.0.1",
                        version=retirement.VERSION, commit=retirement.COMMIT,
                        build_time=retirement.BUILD_TIME, published_at=retirement.BUILD_TIME,
                        platforms={"linux-amd64": entry})
        self.payloads[retirement.LATEST_KEY] = json.dumps(manifest).encode()
        self.payloads[retirement.RELEASE_KEY] = json.dumps(manifest).encode()
        self.payloads[retirement.SUMS_KEY] = "".join(
            hashlib.sha256(self.payloads[key]).hexdigest() + "  linux-amd64/" + key.rsplit("/", 1)[1] + "\n"
            for key in (retirement.IMAGE_KEY, retirement.BINARY_KEY)).encode()
        self.deleted, self.heads = [], []
        self.list_calls = 0
        self.extra = None
        self.fail_delete = None
        self.keep_after_delete = False

    def metadata(self, key):
        return dict(Key=key, Size=len(self.payloads[key]), ETag='"' + hashlib.md5(self.payloads[key]).hexdigest() + '"',
                    LastModified="2026-10-18T12:39:07+00:00")

    def list_page(self, token=None):
        self.list_calls += 1
        keys = sorted(self.payloads)
        middle = 2
        selected = keys[:middle] if token is None else keys[middle:]
        result = dict(IsTruncated=token is None and len(keys) > middle,
                      Contents=[self.metadata(key) for key in selected])
        if result["IsTruncated"]:
            result["NextContinuationToken"] = "second-page"
        if self.extra and token is None:
            result["Contents"].append(self.extra)
        return result

    def download(self, key, destination):
        destination.write_bytes(self.payloads[key])
        return dict(ETag=self.metadata(key)["ETag"])

    def head(self, key):
        self.heads.append(key)
        item = self.metadata(key)
        return dict(ContentLength=item["Size"], ETag=item["ETag"], LastModified=item["LastModified"])

    def delete(self, key):
        if key == self.fail_delete:
            raise retirement.RetirementError("模拟存储失败")
        self.deleted.append(key)
        if not self.keep_after_delete:
            del self.payloads[key]


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MemoryStore()
        # 使用小对象执行完整流程，真实 CLI 的固定大小/摘要默认值保持不变。
        expected = {key: {"size": len(self.store.payloads[key]), "sha256": hashlib.sha256(self.store.payloads[key]).hexdigest()}
                    for key in (retirement.IMAGE_KEY, retirement.BINARY_KEY)}
        self.fixed_packages = patch.object(retirement, "PACKAGE_IDENTITIES", expected)
        self.fixed_packages.start()
        self.addCleanup(self.fixed_packages.stop)
        self.snapshot = self.root / "retirement"
        self.report = self.root / "cleanup/report.json"

    def take_snapshot(self):
        retirement.snapshot(self.store, self.snapshot, bucket="mskdsp-update")
        return retirement.file_sha256(self.snapshot / "inventory.json")

    def apply(self, checksum):
        return retirement.apply(self.store, self.snapshot, checksum, self.report, bucket="mskdsp-update")

    def test_snapshot_and_apply_delete_exact_five_latest_first(self):
        checksum = self.take_snapshot()
        inventory = json.loads((self.snapshot / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(inventory["object_count"], 5)
        self.assertEqual({item["key"] for item in inventory["objects"]}, set(retirement.KEYS))
        self.assertEqual(self.store.deleted, [])
        report = self.apply(checksum)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(self.store.deleted[0], retirement.LATEST_KEY)
        self.assertEqual(set(self.store.deleted), set(retirement.KEYS))
        self.assertEqual(self.store.payloads, {})
        self.assertGreaterEqual(len(self.store.heads), 10)
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8"))["deleted_keys"], self.store.deleted)

    def test_snapshot_rejects_outside_extra_and_missing_objects(self):
        for index, extra in enumerate(("mskdsp-platform/beta/keep", retirement.PREFIX + "extra")):
            with self.subTest(extra=extra):
                self.store.extra = dict(Key=extra, Size=1, ETag='"' + 'a' * 32 + '"', LastModified="2026-10-18T12:39:07+00:00")
                with self.assertRaises(retirement.RetirementError):
                    retirement.snapshot(self.store, self.root / str(index), bucket="mskdsp-update")
        self.store.extra = None
        del self.store.payloads[retirement.SUMS_KEY]
        with self.assertRaises(retirement.RetirementError):
            self.take_snapshot()
        self.assertEqual(self.store.deleted, [])

    def test_duplicate_key_and_repeated_pagination_token_are_rejected(self):
        original = self.store.list_page
        def duplicate(token=None):
            result = original(token)
            if token:
                result["Contents"].append(self.store.metadata(sorted(self.store.payloads)[0]))
            return result
        with patch.object(self.store, "list_page", side_effect=duplicate), self.assertRaises(retirement.RetirementError):
            self.take_snapshot()
        page = dict(IsTruncated=True, NextContinuationToken="repeat", Contents=[])
        with patch.object(self.store, "list_page", return_value=page), self.assertRaises(retirement.RetirementError):
            self.take_snapshot()

    def test_tampered_backup_or_inventory_prevents_every_delete(self):
        checksum = self.take_snapshot()
        path = self.snapshot / "objects" / retirement.BINARY_KEY
        path.write_bytes(b"tampered")
        with self.assertRaises(retirement.RetirementError):
            self.apply(checksum)
        self.assertEqual(self.store.deleted, [])
        with self.assertRaises(retirement.RetirementError):
            self.apply("0" * 64)
        self.assertEqual(self.store.deleted, [])

    def test_remote_change_is_checked_before_every_delete(self):
        checksum = self.take_snapshot()
        self.store.payloads[retirement.IMAGE_KEY] += b"changed"
        with self.assertRaises(retirement.RetirementError):
            self.apply(checksum)
        self.assertEqual(self.store.deleted, [])
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8"))["status"], "failed")

    def test_object_change_after_full_preflight_stops_individual_deletion(self):
        checksum = self.take_snapshot()
        original = self.store.head
        calls = 0
        def head(key):
            nonlocal calls
            calls += 1
            # 五次预检后，第六次恰为 latest 删除前的再次核对。
            if calls == 6:
                self.store.payloads[key] += b"changed"
            return original(key)
        with patch.object(self.store, "head", side_effect=head), self.assertRaises(retirement.RetirementError):
            self.apply(checksum)
        self.assertEqual(self.store.deleted, [])
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8"))["uncertain_keys"], [])

    def test_partial_failure_reports_acknowledged_and_uncertain_keys(self):
        checksum = self.take_snapshot()
        self.store.fail_delete = retirement.RELEASE_KEY
        with self.assertRaises(retirement.RetirementError):
            self.apply(checksum)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["deleted_keys"], [retirement.LATEST_KEY])
        self.assertEqual(report["uncertain_keys"], [retirement.RELEASE_KEY])

    def test_nonempty_final_prefix_is_not_reported_as_success(self):
        checksum = self.take_snapshot()
        self.store.keep_after_delete = True
        with self.assertRaises(retirement.RetirementError):
            self.apply(checksum)
        self.assertEqual(json.loads(self.report.read_text(encoding="utf-8"))["status"], "failed")

    def test_manifest_and_checksums_are_validated_before_inventory(self):
        manifest = json.loads(self.store.payloads[retirement.LATEST_KEY])
        manifest["commit"] = "f" * 40
        self.store.payloads[retirement.LATEST_KEY] = json.dumps(manifest).encode()
        with self.assertRaises(retirement.RetirementError):
            self.take_snapshot()
        self.assertFalse((self.snapshot / "inventory.json").exists())

    def test_checksums_content_mismatch_is_rejected_even_when_download_is_consistent(self):
        self.store.payloads[retirement.SUMS_KEY] = b"incorrect checksums\n"
        with self.assertRaises(retirement.RetirementError):
            self.take_snapshot()
        self.assertFalse((self.snapshot / "inventory.json").exists())

    def test_report_cannot_overwrite_confirmed_snapshot(self):
        checksum = self.take_snapshot()
        with self.assertRaises(retirement.RetirementError):
            retirement.apply(self.store, self.snapshot, checksum, self.snapshot / "inventory.json", bucket="mskdsp-update")
        self.assertEqual(retirement.file_sha256(self.snapshot / "inventory.json"), checksum)
        self.assertEqual(self.store.deleted, [])

    def test_aws_adapter_emits_only_explicit_allowlisted_delete_commands(self):
        environment = dict(R2_ACCOUNT_ID="a" * 32, AWS_ACCESS_KEY_ID="fake-access", AWS_SECRET_ACCESS_KEY="fake-secret")
        commands = []
        def run(argv, **options):
            self.assertNotIn("fake-secret", repr(argv))
            self.assertEqual(options["env"]["AWS_SECRET_ACCESS_KEY"], "fake-secret")
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="{}")
        with patch.dict(os.environ, environment, clear=True), patch.object(subprocess, "run", side_effect=run):
            store = retirement.AwsStore("mskdsp-update")
            for key in retirement.KEYS:
                store.delete(key)
            with self.assertRaises(retirement.RetirementError):
                store.delete("mskdsp-platform/beta/keep")
        self.assertEqual(len(commands), 5)
        self.assertTrue(all(command[:3] == ["aws", "s3api", "delete-object"] for command in commands))
        self.assertEqual([command[command.index("--key") + 1] for command in commands], list(retirement.KEYS))

    def test_aws_errors_hide_command_credentials_and_endpoint(self):
        environment = dict(R2_ACCOUNT_ID="a" * 32, AWS_ACCESS_KEY_ID="fake-access", AWS_SECRET_ACCESS_KEY="fake-secret")
        failure = subprocess.CalledProcessError(1, ["aws", "fake-secret"], stderr="fake-access endpoint-secret")
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(subprocess, "run", side_effect=failure), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(retirement.RetirementError) as error:
                retirement.AwsStore("mskdsp-update").list_page()
        combined = str(error.exception) + output.getvalue()
        for secret in ("fake-access", "fake-secret", "endpoint-secret", "a" * 32):
            self.assertNotIn(secret, combined)


if __name__ == "__main__":
    unittest.main()

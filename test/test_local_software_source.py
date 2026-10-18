"""本地已入库平台 CI 包选择与独立复制验证。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import local_software_source as local
import update_common as common


def metadata(version="0.0.1-ci.20261013t080000.12.1+sha.abcdef1", raw=b"archive"):
    return dict(product=common.PROJECT, platform="linux-amd64", channel="ci", version=version,
                file_name="binary.tar.gz", size=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                source_ref="main", source_sha="abcdef1" + "0" * 33, build_time="2026-10-13T08:00:00Z")


class LocalSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "software"
        self.source = local.LocalSoftwareSource(self.root, "ci")

    def insert(self, value, raw=b"archive", created="2026-10-13T08:00:00Z", indexed=None):
        self.root.mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.root / "software.sqlite3")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS packages(id TEXT PRIMARY KEY, metadata TEXT, product TEXT, version TEXT, platform TEXT, created TEXT)")
            db.execute("INSERT INTO packages VALUES(?,?,?,?,?,?)", (str(db.execute("SELECT count(*) FROM packages").fetchone()[0]), json.dumps(value), *(indexed or (value["product"], value["version"], value["platform"])), created))
        if value["file_name"] not in ("../escape", "bad\\name"):
            path = self.root / "packages" / value["product"] / value["version"] / value["platform"] / value["file_name"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

    def test_missing_repository_is_empty_and_never_created(self):
        self.assertIsNone(self.source.latest())
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        self.assertIsNone(self.source.latest())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_chooses_version_order_not_late_arrival_and_reads_without_mutation(self):
        new = metadata("0.0.1-ci.20261014t080000.2.1+sha.abcdef1")
        self.insert(new, created="2026-10-14T08:00:00Z")
        self.insert(metadata(), created="2026-10-18T08:00:00Z")
        before = (self.root / "software.sqlite3").read_bytes()
        manifest, artifact = self.source.latest()
        self.assertEqual(manifest["version"], new["version"])
        self.assertEqual(manifest["commit"], new["source_sha"])
        self.assertEqual(artifact["binary_sha256"], new["sha256"])
        self.assertNotIn("binary_url", artifact)
        self.assertEqual((self.root / "software.sqlite3").read_bytes(), before)

    def test_uncompleted_upload_and_other_products_are_not_candidates(self):
        self.root.mkdir()
        with closing(sqlite3.connect(self.root / "software.sqlite3")) as db, db:
            db.execute("CREATE TABLE packages(id TEXT PRIMARY KEY, metadata TEXT, product TEXT, version TEXT, platform TEXT, created TEXT)")
            db.execute("CREATE TABLE uploads(metadata TEXT, state TEXT)")
            db.execute("INSERT INTO uploads VALUES(?, 'uploading')", (json.dumps(metadata()),))
        other = metadata()
        other.update(product="mskdsp-lower", channel="")
        self.insert(other)
        self.assertIsNone(self.source.latest())

    def test_invalid_identity_index_or_path_is_rejected(self):
        for index, (field, value) in enumerate((("file_name", "../escape"), ("file_name", "bad\\name"), ("source_ref", "dev"),
                             ("source_sha", "a" * 40), ("build_time", "2026-10-13"), ("sha256", "bad"), ("size", True))):
            with self.subTest(field=field):
                self.root = Path(self.temp.name) / ("invalid-" + str(index))
                self.source = local.LocalSoftwareSource(self.root)
                bad = metadata()
                bad[field] = value
                self.insert(bad)
                with self.assertRaises(common.UpdateError):
                    self.source.latest()
        self.root = Path(self.temp.name) / "mismatched-index"
        self.source = local.LocalSoftwareSource(self.root)
        self.insert(metadata(), indexed=(common.PROJECT, "wrong", "linux-amd64"))
        with self.assertRaises(common.UpdateError):
            self.source.latest()

    def test_active_wal_commits_are_visible_without_database_writes(self):
        self.root.mkdir()
        with closing(sqlite3.connect(self.root / "software.sqlite3")) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.commit()
            self.insert(metadata())
            database = self.root / "software.sqlite3"
            before = database.read_bytes()
            self.assertEqual(self.source.latest()[0]["version"], metadata()["version"])
            self.assertEqual(database.read_bytes(), before)

    def test_unsupported_channel_is_rejected_and_other_channel_is_ignored(self):
        with self.assertRaises(common.UpdateError):
            local.LocalSoftwareSource(self.root, "stable")
        other = metadata()
        other["channel"] = "beta"
        self.insert(other)
        self.assertIsNone(self.source.latest())

    def test_copy_checks_snapshot_size_digest_and_keeps_store_untouched(self):
        self.insert(metadata())
        manifest, artifact = self.source.latest()
        with tempfile.TemporaryDirectory() as temp:
            path = self.source.copy_artifact(artifact, temp)
            self.assertEqual(path.read_bytes(), b"archive")
            self.assertTrue(path.is_relative_to(Path(temp)))
            path.unlink()
            origin = self.root / "packages" / common.PROJECT / manifest["version"] / "linux-amd64" / "binary.tar.gz"
            origin.write_bytes(b"changed")
            with self.assertRaises(common.UpdateError):
                self.source.copy_artifact(artifact, temp)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_database_and_artifact_links_are_rejected(self):
        self.insert(metadata())
        _, artifact = self.source.latest()
        original = self.root / "packages" / common.PROJECT / metadata()["version"] / "linux-amd64" / "binary.tar.gz"
        real = self.root / "real.tar.gz"
        original.rename(real)
        try:
            original.symlink_to(real)
        except OSError:
            self.skipTest("当前账户不能创建符号链接")
        with self.assertRaises(common.UpdateError):
            self.source.latest()
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(common.UpdateError):
            self.source.copy_artifact(artifact, temp)
        original.unlink()
        db = self.root / "software.sqlite3"
        real_db = self.root / "real.sqlite3"
        db.rename(real_db)
        db.symlink_to(real_db)
        with self.assertRaises(common.UpdateError):
            self.source.latest()


if __name__ == "__main__":
    unittest.main()

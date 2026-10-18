"""无需 Docker daemon 的 GitLab 交付准备测试。"""
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import gitlab_package as package
from script.release import build_metadata, sha256_file

TARGET = "usr/local/bin/mskdsp-platform"


def layer_bytes(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        for name, content in entries:
            item = tarfile.TarInfo(name)
            if isinstance(content, bytes):
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
            else:
                item.type = tarfile.SYMTYPE
                item.linkname = content
                archive.addfile(item)
    return result.getvalue()


class GitlabPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.metadata = build_metadata("ci", "0.0.1", "0123456789abcdef0123456789abcdef01234567",
                                       "2026-10-16T08:00:00Z", "42.1", "owner/repo")
        self.meta = self.root / "metadata.json"
        self.meta.write_text(json.dumps(self.metadata), encoding="utf-8")
        self.image = self.root / "image.tar"
        self.identity = {"service": "mskdsp-platform", **{key: self.metadata[key] for key in ("version", "channel", "commit", "build_time")}}
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)
        for name, text in (("config.toml", "[server]\n"), ("run.sh", "docker run example\n"),
                           ("updater.json", '{"manifest_url":"https://updates.example/mskdsp-platform/ci/latest.json"}')):
            (self.source / "deploy" / name).write_text(text, encoding="utf-8")

    def image_with(self, layers, *, arch="amd64", labels=None, tags=None, digest_valid=True):
        config = json.dumps({"os": "linux", "architecture": arch, "config": {"Labels": labels or {
            "org.opencontainers.image.version": self.metadata["version"],
            "org.opencontainers.image.revision": self.metadata["commit"], "io.mskdsp.channel": "ci"}},
            "rootfs": {"diff_ids": ["sha256:" + hashlib.sha256(gzip.decompress(layer) if layer.startswith(b"\x1f\x8b") else layer).hexdigest() for layer in layers]}}).encode()
        config_name = (hashlib.sha256(config).hexdigest() if digest_valid else "0" * 64) + ".json"
        names = [f"layer-{index}/layer.tar" for index in range(len(layers))]
        manifest = json.dumps([{"Config": config_name, "RepoTags": tags or [self.metadata["image"]], "Layers": names}]).encode()
        with tarfile.open(self.image, "w") as archive:
            for name, data in [("manifest.json", manifest), (config_name, config), *zip(names, layers)]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def prepare(self, suffix="one"):
        return package.prepare(metadata=self.meta, image_tar=self.image, public_base_url="https://updates.example",
                               package_dir=self.root / suffix / "package", r2_output=self.root / suffix / "r2",
                               bundle_output=self.root / suffix / "bundle", release_output=self.root / suffix / "release",
                               bundle_source=self.source)

    def test_complete_delivery_and_stable_gzip(self):
        self.image_with([layer_bytes([(TARGET, b"old")]), layer_bytes([(TARGET, b"final executable")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            manifest = self.prepare()
            self.prepare("two")
        first, second = self.root / "one", self.root / "two"
        self.assertEqual((first / "package/mskdsp-platform").read_bytes(), b"final executable")
        compressed = (first / "package/image.tar.gz").read_bytes()
        self.assertEqual(compressed, (second / "package/image.tar.gz").read_bytes())
        self.assertEqual(gzip.decompress(compressed), self.image.read_bytes())
        self.assertEqual(compressed[4:8], b"\0\0\0\0")
        release = first / "release"
        self.assertEqual(json.loads((release / "updater-manifest.json").read_text()), manifest)
        with zipfile.ZipFile(next(release.glob("*.zip"))) as zipped:
            self.assertEqual(zipped.read("image.tar.gz"), compressed)
        for line in (release / "SHA256SUMS").read_text().splitlines():
            sha, name = line.split("  ")
            self.assertEqual(sha, sha256_file(release / name))
        self.assertEqual(len(list(release.glob("*-binary.tar.gz"))), 1)

    def test_wrong_image_identity_rejected_before_binary_execution(self):
        for index, changes in enumerate(({"arch": "arm64"}, {"tags": ["other:tag"]}, {"labels": {"io.mskdsp.channel": "beta"}}, {"digest_valid": False})):
            with self.subTest(changes=changes):
                self.image_with([layer_bytes([(TARGET, b"binary")])], **changes)
                with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaises(ValueError):
                    self.prepare(f"case-{index}")
                identity.assert_not_called()
                self.assertFalse((self.root / f"case-{index}/release").exists())

    def test_wrong_binary_identity_rejected(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value={}), self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / "one/release").exists())

    def test_whiteouts_remove_previous_binary(self):
        for index, marker in enumerate(("usr/local/bin/.wh.mskdsp-platform", "usr/local/.wh.bin", "usr/.wh..wh..opq", ".wh.usr")):
            with self.subTest(marker=marker):
                self.image_with([layer_bytes([(TARGET, b"old")]), layer_bytes([(marker, b"")])])
                with self.assertRaises(ValueError):
                    self.prepare(f"case-{index}")

    def test_whiteout_does_not_delete_same_layer_replacement(self):
        self.image_with([layer_bytes([(TARGET, b"old")]), layer_bytes([(TARGET, b"new"), ("usr/local/bin/.wh.mskdsp-platform", b"")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            self.prepare()
        self.assertEqual((self.root / "one/package/mskdsp-platform").read_bytes(), b"new")

    def test_unrelated_whiteout_keeps_binary(self):
        self.image_with([layer_bytes([(TARGET, b"keep")]), layer_bytes([("etc/.wh..wh..opq", b"")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            self.prepare()

    def test_compressed_buildkit_layer_checks_uncompressed_digest(self):
        self.image_with([gzip.compress(layer_bytes([(TARGET, b"compressed layer executable")]), mtime=0)])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            self.prepare()
        self.assertEqual((self.root / "one/package/mskdsp-platform").read_bytes(), b"compressed layer executable")

    def test_links_and_unsafe_paths_rejected(self):
        for index, entries in enumerate(([(TARGET, "/evil")], [("usr/local/bin", "/elsewhere"), (TARGET, b"binary")],
                                        [("../../outside", b"bad"), (TARGET, b"binary")])):
            with self.subTest(entries=entries):
                self.image_with([layer_bytes(entries)])
                with self.assertRaises(ValueError):
                    self.prepare(f"case-{index}")

    def test_layer_digest_mismatch_rejected_before_binary_execution(self):
        self.image_with([layer_bytes([(TARGET, b"good executable")])])
        content = self.image.read_bytes().replace(b"good executable", b"evil executable")
        self.image.write_bytes(content)
        with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaisesRegex(ValueError, "镜像层内容"):
            self.prepare()
        identity.assert_not_called()

    def test_cli_contract(self):
        argv = ["gitlab_package.py", "prepare", "--metadata", str(self.meta), "--image-tar", str(self.image),
                "--public-base-url", "https://updates.example", "--prefix", "mskdsp-platform"]
        with patch.object(sys, "argv", argv), patch.object(package, "prepare") as prepare:
            package.main()
        self.assertEqual(prepare.call_args.kwargs["image_tar"], self.image)
        self.assertEqual(prepare.call_args.kwargs["package_dir"], Path("package"))


if __name__ == "__main__":
    unittest.main()

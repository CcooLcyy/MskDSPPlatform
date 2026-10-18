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
        self.metadata.update(ref="main", source="https://gitlab.test/platform")
        self.meta = self.root / "metadata.json"
        self.meta.write_text(json.dumps(self.metadata), encoding="utf-8")
        self.image = self.root / "image.tar"
        self.identity = {"service": "mskdsp-platform", **{key: self.metadata[key] for key in ("version", "channel", "commit", "build_time")}}
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)
        for name, text in (("config.toml", "[server]\n"), ("run.sh", "docker run example\n"),
                           ("updater.json", '{"source":"platform","channel":"ci"}')):
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
        return package.prepare_platform(metadata=self.meta, image_tar=self.image,
                                        package_dir=self.root / suffix / "package")

    def offline(self, suffix="one"):
        return package.offline_platform(metadata=self.meta, package_dir=self.root / suffix / "package",
                                        bundle_source=self.source, bundle_output=self.root / suffix / "bundle",
                                        release_output=self.root / suffix / "release")

    def test_lightweight_prepare_and_manual_offline_delivery(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            expected = self.prepare()
        self.assertTrue((self.root / "one/package/upload-metadata.json").is_file())
        self.assertFalse((self.root / "one/bundle").exists())
        self.assertFalse((self.root / "one/release").exists())
        self.image.unlink()
        with patch.object(package.publish_r2, "read_binary_identity") as identity:
            self.offline()
        identity.assert_not_called()
        self.assertEqual(json.loads((self.root / "one/package/upload-metadata.json").read_text()), expected)
        self.assertTrue(next((self.root / "one/release").glob("*.zip")).is_file())

    def test_single_image_inspection_and_streaming_asset_writes(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        original = Path.read_bytes
        def forbid_large_read(path):
            if path.name.endswith((".tar.gz", ".tar")):
                self.fail("归档必须流式处理")
            return original(path)
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity), \
             patch.object(package, "inspect_archive", wraps=package.inspect_archive) as inspect, \
             patch.object(Path, "read_bytes", forbid_large_read):
            self.prepare()
        self.assertEqual(inspect.call_count, 1)

    def test_manual_offline_rejects_tampered_cross_job_assets(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            self.prepare()
        (self.root / "one/package/image.tar.gz").write_bytes(gzip.compress(self.image.read_bytes() + b"tampered", mtime=0))
        with self.assertRaisesRegex(ValueError, "离线镜像摘要"):
            self.offline()
        self.assertFalse((self.root / "one/release").exists())

    def test_manual_offline_rejects_unrelated_metadata(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            self.prepare()
        self.metadata["commit"] = "f" * 40
        self.meta.write_text(json.dumps(self.metadata), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.offline()
        self.assertFalse((self.root / "one/release").exists())

    def test_manual_offline_rejects_tampered_upload_metadata(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            self.prepare()
        (self.root / "one/package/upload-metadata.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "上传元数据"):
            self.offline()
        self.assertFalse((self.root / "one/release").exists())

    def test_complete_delivery_and_stable_gzip(self):
        self.image_with([layer_bytes([(TARGET, b"old")]), layer_bytes([(TARGET, b"final executable")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            manifest = self.prepare()
            self.prepare("two")
        self.offline()
        first, second = self.root / "one", self.root / "two"
        self.assertEqual((first / "package/mskdsp-platform").read_bytes(), b"final executable")
        compressed = (first / "package/image.tar.gz").read_bytes()
        self.assertEqual(compressed, (second / "package/image.tar.gz").read_bytes())
        self.assertEqual(gzip.decompress(compressed), self.image.read_bytes())
        self.assertEqual(compressed[4:8], b"\0\0\0\0")
        release = first / "release"
        self.assertEqual(json.loads((first / "package/upload-metadata.json").read_text()), manifest)
        with zipfile.ZipFile(next(release.glob("*.zip"))) as zipped:
            self.assertEqual(zipped.read("image.tar.gz"), compressed)
            self.assertEqual(zipped.getinfo("image.tar.gz").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(json.loads(zipped.read("metadata.json")), self.metadata)
            self.assertEqual(json.loads(zipped.read("deploy/updater.json")), {"source": "platform", "channel": "ci"})
        for directory in (first / "bundle", release):
            for line in (directory / "SHA256SUMS").read_text().splitlines():
                sha, name = line.split("  ")
                self.assertEqual(sha, sha256_file(directory / name))
        self.assertEqual((release / "binary.tar.gz").read_bytes(), (first / "package/binary.tar.gz").read_bytes())
        self.assertEqual((first / "package/binary.tar.gz").read_bytes(), (second / "package/binary.tar.gz").read_bytes())

    def test_wrong_image_identity_rejected_before_binary_execution(self):
        for index, changes in enumerate(({"arch": "arm64"}, {"tags": ["other:tag"]}, {"labels": {"io.mskdsp.channel": "beta"}}, {"digest_valid": False})):
            with self.subTest(changes=changes):
                self.image_with([layer_bytes([(TARGET, b"binary")])], **changes)
                with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaises(ValueError):
                    self.prepare(f"case-{index}")
                identity.assert_not_called()
                self.assertFalse((self.root / f"case-{index}/package/upload-metadata.json").exists())

    def test_wrong_binary_identity_rejected(self):
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        with patch.object(package.publish_r2, "read_binary_identity", return_value={}), self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / "one/package/upload-metadata.json").exists())

    def test_whiteouts_remove_previous_binary(self):
        for index, marker in enumerate(("usr/local/bin/.wh.mskdsp-platform", "usr/local/.wh.bin", "usr/.wh..wh..opq", ".wh.usr")):
            with self.subTest(marker=marker):
                self.image_with([layer_bytes([(TARGET, b"old")]), layer_bytes([(marker, b"")])])
                with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaises(ValueError):
                    self.prepare(f"case-{index}")
                identity.assert_not_called()

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
                with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaises(ValueError):
                    self.prepare(f"case-{index}")
                identity.assert_not_called()

    def test_layer_digest_mismatch_rejected_before_binary_execution(self):
        self.image_with([layer_bytes([(TARGET, b"good executable")])])
        content = self.image.read_bytes().replace(b"good executable", b"evil executable")
        self.image.write_bytes(content)
        with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaisesRegex(ValueError, "镜像层内容"):
            self.prepare()
        identity.assert_not_called()

    def test_layer_digest_covers_bytes_after_tar_eof(self):
        layer = layer_bytes([(TARGET, b"binary")]) + b"\0" * 65536 + b"original tail"
        self.image_with([layer])
        self.image.write_bytes(self.image.read_bytes().replace(b"original tail", b"tampered tail"))
        with patch.object(package.publish_r2, "read_binary_identity") as identity, self.assertRaisesRegex(ValueError, "镜像层内容"):
            self.prepare()
        identity.assert_not_called()

    def test_offline_cli_contract(self):
        argv = ["gitlab_package.py", "offline-platform", "--metadata", str(self.meta), "--package-dir", "prepared"]
        with patch.object(sys, "argv", argv), patch.object(package, "offline_platform") as offline:
            package.main()
        self.assertEqual(offline.call_args.kwargs["package_dir"], Path("prepared"))
        self.assertEqual(offline.call_args.kwargs["release_output"], Path("release"))

    def test_cli_contract(self):
        argv = ["gitlab_package.py", "prepare-platform", "--metadata", str(self.meta), "--image-tar", str(self.image)]
        with patch.object(sys, "argv", argv), patch.object(package, "prepare_platform") as prepare:
            package.main()
        self.assertEqual(prepare.call_args.kwargs["image_tar"], self.image)
        self.assertEqual(prepare.call_args.kwargs["package_dir"], Path("package"))


class PlatformPackageTests(unittest.TestCase):
    setUp = GitlabPackageTests.setUp
    image_with = GitlabPackageTests.image_with
    def test_platform_preparation_has_no_download_domain_dependency(self):
        self.metadata.update(ref="main", source="https://gitlab.test/platform")
        self.meta.write_text(json.dumps(self.metadata), encoding="utf-8")
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        output = self.root / "platform-package"
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            result = package.prepare_platform(metadata=self.meta, image_tar=self.image, package_dir=output)
        self.assertEqual(result["product"], "mskdsp-platform")
        self.assertEqual(result["channel"], "ci")
        self.assertEqual(result["source_ref"], "main")
        self.assertEqual(result["size"], (output / "binary.tar.gz").stat().st_size)
        self.assertEqual(result["sha256"], sha256_file(output / "binary.tar.gz"))
        self.assertEqual(gzip.decompress((output / "image.tar.gz").read_bytes()), self.image.read_bytes())
        self.assertEqual(json.loads((output / "upload-metadata.json").read_text(encoding="utf-8")), result)
        self.assertNotIn("url", json.dumps(result))
        with tarfile.open(output / "binary.tar.gz") as archive:
            self.assertEqual(archive.getnames(), ["mskdsp-platform"])

    def test_ci_yaml_replaces_r2_with_platform_upload_and_keeps_bootstrap_image(self):
        root = Path(__file__).resolve().parents[1]
        configuration = (root / ".gitlab-ci.yml").read_text(encoding="utf-8") + (root / ".gitlab/ci/ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("R2_", configuration)
        self.assertNotIn("publish-r2", configuration)
        self.assertIn("upload-platform:", configuration)
        self.assertIn("prepare-platform", configuration)
        self.assertIn("package/image.tar", configuration)

    def test_platform_offline_bootstrap_is_independent_and_rechecks_digest(self):
        self.metadata.update(ref="main", source="https://gitlab.test/platform")
        self.meta.write_text(json.dumps(self.metadata), encoding="utf-8")
        self.image_with([layer_bytes([(TARGET, b"binary")])])
        output = self.root / "platform-package"
        with patch.object(package.publish_r2, "read_binary_identity", return_value=self.identity):
            package.prepare_platform(metadata=self.meta, image_tar=self.image, package_dir=output)
        bundle, delivery = self.root / "offline", self.root / "delivery"
        package.offline_platform(metadata=self.meta, package_dir=output, bundle_source=self.source,
                                 bundle_output=bundle, release_output=delivery)
        self.assertEqual(json.loads((bundle / "deploy/updater.json").read_text()),
                         {"source": "platform", "channel": "ci"})
        with zipfile.ZipFile(next(delivery.glob("*.zip"))) as archive:
            self.assertEqual(archive.read("image.tar.gz"), (output / "image.tar.gz").read_bytes())
        with (output / "binary.tar.gz").open("ab") as corrupted:
            corrupted.write(b"corrupt")
        with self.assertRaisesRegex(ValueError, "元数据"):
            package.offline_platform(metadata=self.meta, package_dir=output, bundle_source=self.source,
                                     bundle_output=self.root / "other-offline", release_output=self.root / "other-delivery")
        self.assertFalse((self.root / "other-offline").exists())


if __name__ == "__main__":
    unittest.main()

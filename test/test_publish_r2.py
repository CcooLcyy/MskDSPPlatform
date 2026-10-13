"""R2 交付资产、入口提交顺序及限定通道清理的回归测试。"""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import publish_r2 as release
from script import release as bundle_release
from script.release import build_metadata
from script.update_common import validate_manifest, version_order

SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = "https://updates.example"
SECRETS = {"R2_ACCOUNT_ID": "a" * 32, "AWS_ACCESS_KEY_ID": "test-key", "AWS_SECRET_ACCESS_KEY": "test-secret"}


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metadata = build_metadata("stable", "0.0.1", SHA, "2026-10-13T08:00:00Z", "42.1", "owner/repo")
        self.meta_path = self.root / "metadata.json"
        self.meta_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        self.binary = self.root / "mskdsp-platform"
        self.binary.write_bytes(b"verified executable")
        self.image = self.root / "image.tar.gz"
        self.write_image()
        self.output = self.root / "r2"

    def write_image(self):
        config = json.dumps({"os": "linux", "architecture": "amd64", "config": {"Labels": {
            "org.opencontainers.image.version": self.metadata["version"],
            "org.opencontainers.image.revision": SHA, "io.mskdsp.channel": self.metadata["channel"]}}}).encode()
        config_name = hashlib.sha256(config).hexdigest() + ".json"
        manifest = json.dumps([{"Config": config_name, "RepoTags": [self.metadata["image"]], "Layers": ["layer.tar"]}]).encode()
        with tarfile.open(self.image, "w:gz") as archive:
            for name, data in (("manifest.json", manifest), (config_name, config), ("layer.tar", b"layer")):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **changes):
        args = dict(metadata=self.meta_path, image_archive=self.image, binary=self.binary,
                    output=self.output, public_base_url=BASE,
                    identity_reader=lambda _: {"service": "mskdsp-platform", **{key: self.metadata[key] for key in ("version", "channel", "commit", "build_time")}})
        return release.prepare(**(args | changes))

    def publish(self):
        return release.publish(output_dir=self.output, bucket="mskdsp-update", public_base_url=BASE)

    def bundle_source(self):
        source = self.root / "source"
        (source / "deploy").mkdir(parents=True)
        (source / "deploy/config.toml").write_text("[server]\n", encoding="utf-8")
        (source / "deploy/run.sh").write_text("docker run example\n", encoding="utf-8")
        (source / "deploy/updater.json").write_text(json.dumps({"manifest_url": BASE + "/mskdsp-platform/stable/latest.json"}), encoding="utf-8")
        return source

    # 联合准备复用镜像校验和摘要，交付文件与独立命令完全一致。
    def test_combined_prepare_reuses_image_checks_and_preserves_bundle(self):
        source = self.bundle_source()
        combined = self.root / "combined"
        standalone = self.root / "standalone"
        with patch.object(release, "inspect_archive", wraps=release.inspect_archive) as r2_inspect, \
             patch.object(bundle_release, "inspect_archive", wraps=bundle_release.inspect_archive) as bundle_inspect, \
             patch.object(release, "digest", wraps=release.digest) as r2_digest, \
             patch.object(bundle_release, "sha256_file", wraps=bundle_release.sha256_file) as bundle_digest:
            manifest = self.prepare(bundle_output=combined, bundle_source=source)
        self.assertEqual(r2_inspect.call_count + bundle_inspect.call_count, 1)
        image_hash_calls = [call for call in (*r2_digest.call_args_list, *bundle_digest.call_args_list)
                            if call.args[0].name == "image.tar.gz" or call.args[0].name.endswith("-image.tar.gz")]
        self.assertEqual(len(image_hash_calls), 1)
        bundle_release.write_bundle(self.metadata, self.image, standalone, source)
        expected_files = {path.relative_to(standalone) for path in standalone.rglob("*") if path.is_file()}
        self.assertEqual(expected_files, {path.relative_to(combined) for path in combined.rglob("*") if path.is_file()})
        for name in expected_files:
            self.assertEqual((combined / name).read_bytes(), (standalone / name).read_bytes(), str(name))
        sums = (combined / "SHA256SUMS").read_text(encoding="utf-8")
        for line in sums.splitlines():
            sha, name = line.split("  ", 1)
            self.assertEqual(sha, release.digest(combined / name))
        self.assertEqual(json.loads((combined / "latest.json").read_text(encoding="utf-8"))["image_archive"]["sha256"],
                         manifest["platforms"]["linux-amd64"]["sha256"])

    # 联合准备不能因复用校验而接受身份错误的镜像或产生交付目录。
    def test_combined_prepare_rejects_wrong_image_identity(self):
        source = self.bundle_source()
        self.metadata["image"] = self.metadata["image"].replace(":0.0.1", ":0.0.2")
        self.write_image()
        combined = self.root / "combined"
        with self.assertRaises(ValueError):
            self.prepare(bundle_output=combined, bundle_source=source)
        self.assertFalse(self.output.exists())
        self.assertFalse(combined.exists())

    # 工作流使用的命令行参数同时生成 R2 资产和离线交付包。
    def test_prepare_cli_generates_both_deliveries(self):
        source = self.bundle_source()
        combined = self.root / "combined"
        args = ["publish_r2.py", "prepare", "--metadata", str(self.meta_path),
                "--image-archive", str(self.image), "--binary", str(self.binary),
                "--output", str(self.output), "--public-base-url", BASE,
                "--bundle-output", str(combined), "--bundle-source", str(source)]
        identity = {"service": "mskdsp-platform", **{key: self.metadata[key] for key in ("version", "channel", "commit", "build_time")}}
        with patch.object(sys, "argv", args), patch.object(release, "read_binary_identity", return_value=identity):
            release.main()
        self.assertTrue((self.output / "release.json").is_file())
        self.assertTrue((combined / "image.tar.gz").is_file())
        self.assertEqual((combined / "image.tar.gz").read_bytes(), self.image.read_bytes())

    # 程序归档只含一个普通可执行文件，镜像归档及清单中的摘要和长度准确。
    def test_prepare_tar_and_manifest(self):
        manifest = self.prepare()
        self.assertEqual(manifest["project"], "mskdsp-platform")
        self.assertEqual(manifest["published_at"], self.metadata["build_time"])
        self.assertEqual(set(manifest["platforms"]), {"linux-amd64"})
        entry = manifest["platforms"]["linux-amd64"]
        for marker in ("", "binary_"):
            path = self.output / "assets" / "0.0.1" / "linux-amd64" / entry[marker + "url"].rsplit("/", 1)[1]
            self.assertEqual(entry[marker + "sha256"], release.digest(path))
            self.assertEqual(entry[marker + "size"], path.stat().st_size)
        binary_tar = next(self.output.rglob("*-binary.tar.gz"))
        with tarfile.open(binary_tar, "r:gz") as archive:
            self.assertEqual(archive.getnames(), ["mskdsp-platform"])
            member = archive.getmember("mskdsp-platform")
            self.assertTrue(member.isfile())
            self.assertEqual(member.mode, 0o755)
            self.assertEqual(archive.extractfile(member).read(), self.binary.read_bytes())
        before = release.digest(binary_tar)
        self.prepare()
        self.assertEqual(release.digest(binary_tar), before)
        self.assertEqual(validate_manifest(manifest, BASE + "/mskdsp-platform/stable/latest.json"), entry)

    # 三通道清单均兼容更新器，构建编号与重跑编号按数字排序。
    def test_ci_beta_format_and_runtime_compatibility(self):
        for channel in ("ci", "beta"):
            self.metadata = build_metadata(channel, "0.0.1", SHA, "2026-10-13T08:00:00Z", "42.10", "owner/repo")
            self.meta_path.write_text(json.dumps(self.metadata), encoding="utf-8")
            self.write_image()
            manifest = self.prepare(output=self.root / channel)
            validate_manifest(manifest, BASE + f"/mskdsp-platform/{channel}/latest.json")
            self.assertIn(".42.10-sha.0123456/linux-amd64/", manifest["platforms"]["linux-amd64"]["binary_url"])
            self.assertGreater(version_order(channel, manifest["version"]), version_order(channel, manifest["version"].replace(".42.10+", ".42.2+")))

    # 程序身份不匹配时不得写入交付目录。
    def test_wrong_binary_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.prepare(identity_reader=lambda _: {"version": "wrong"})
        self.assertFalse(self.output.exists())

    # 同一个正式版本不能变更程序或提交。
    def test_prepare_same_version_content_conflict(self):
        self.prepare()
        self.binary.write_bytes(b"different executable")
        with self.assertRaises(ValueError):
            self.prepare()

    # 拒绝明文、认证信息、参数、根路径及不安全发布前缀。
    def test_urls_and_prefix_are_restricted(self):
        for base in ("http://updates.example", "https://user:password@updates.example", BASE + "/path", BASE + "?key=x"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.prepare(public_base_url=base)
        for prefix in ("", "../other", "/other", "platform//ci", "platform/../other"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.prepare(prefix=prefix)

    # 所有不可变资产验证后才写入口，且入口确认后才清理。
    def test_publish_latest_last_and_cleanup_after_verification(self):
        manifest = self.prepare()
        events = []
        def upload(path, key, *args):
            events.append(("upload", key))
        def verify(url, *args):
            events.append(("verify", url))
        def cleanup(**kwargs):
            events.append(("cleanup", kwargs["channel_root"]))
        with patch.dict(os.environ, SECRETS, clear=True), patch.object(release, "get_json", side_effect=[None, None, manifest]), \
             patch.object(release, "upload", side_effect=upload), patch.object(release, "verify_asset", side_effect=verify), \
             patch.object(release, "cleanup_old_versions", side_effect=cleanup):
            self.publish()
        uploads = [key for event, key in events if event == "upload"]
        self.assertEqual(uploads[-2:], ["mskdsp-platform/stable/0.0.1/release.json", "mskdsp-platform/stable/latest.json"])
        self.assertEqual(events[-1][0], "cleanup")

    # 下载摘要失败或上传失败不能推进 latest 或清理旧版本。
    def test_failure_does_not_advance_latest(self):
        self.prepare()
        for failed_tool in ("upload", "verify_asset"):
            with self.subTest(tool=failed_tool), patch.dict(os.environ, SECRETS, clear=True), \
                 patch.object(release, "get_json", side_effect=[None, None]), \
                 patch.object(release, "upload") as upload, patch.object(release, "verify_asset") as verify, \
                 patch.object(release, "cleanup_old_versions") as cleanup:
                (upload if failed_tool == "upload" else verify).side_effect = ValueError("failure")
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertFalse(any(call.args[1].endswith("latest.json") for call in upload.call_args_list))
                cleanup.assert_not_called()

    # 已发布版本身份冲突、版本倒退均在上传前拒绝。
    def test_remote_conflict_and_downgrade_rejected(self):
        manifest = self.prepare()
        for existing, current in ((manifest | {"commit": "b" * 40}, None), (None, manifest | {"version": "0.0.2"})):
            with patch.dict(os.environ, SECRETS, clear=True), patch.object(release, "get_json", side_effect=[existing, current]), \
                 patch.object(release, "upload") as upload:
                with self.assertRaises(ValueError):
                    self.publish()
                upload.assert_not_called()

    # 重跑已完成版本只再次验证公开资产及入口，不改写不可变文件。
    def test_existing_release_reuses_immutable_assets(self):
        manifest = self.prepare()
        with patch.dict(os.environ, SECRETS, clear=True), patch.object(release, "get_json", side_effect=[manifest, manifest, manifest]), \
             patch.object(release, "upload") as upload, patch.object(release, "verify_asset") as verify, \
             patch.object(release, "cleanup_old_versions"):
            self.publish()
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(upload.call_args.args[1], "mskdsp-platform/stable/latest.json")
        self.assertEqual(upload.call_args.args[-1], "no-store")
        self.assertEqual(verify.call_count, 3)

    # 清单与本地资产不一致时，不得上传或读取远端入口。
    def test_tampered_local_asset_rejected(self):
        self.prepare()
        next(self.output.rglob("*-binary.tar.gz")).write_bytes(b"tampered")
        with patch.dict(os.environ, SECRETS, clear=True), patch.object(release, "get_json") as read:
            with self.assertRaises(ValueError):
                self.publish()
            read.assert_not_called()

    # 最新入口读回失败时仍保留旧资产，不执行清理。
    def test_latest_readback_failure_preserves_old_assets(self):
        self.prepare()
        with patch.dict(os.environ, SECRETS, clear=True), patch.object(release, "get_json", side_effect=[None, None, {}]), \
             patch.object(release, "upload"), patch.object(release, "verify_asset"), \
             patch.object(release, "cleanup_old_versions") as cleanup:
            with self.assertRaises(ValueError):
                self.publish()
            cleanup.assert_not_called()

    # 缺失凭据必须在联网或调用 AWS 前失败，账号不接受命令行明文值。
    def test_missing_secrets_rejected_before_network(self):
        self.prepare()
        for absent in SECRETS:
            with patch.dict(os.environ, {key: value for key, value in SECRETS.items() if key != absent}, clear=True), \
                 patch.object(release, "get_json") as read:
                with self.assertRaises(ValueError):
                    self.publish()
                read.assert_not_called()

    # 命令行账号只能核对环境值，AWS 调用的兼容配置也只能通过环境设置。
    def test_account_and_aws_environment(self):
        self.prepare()
        with patch.dict(os.environ, SECRETS, clear=True), patch.object(release, "get_json") as read:
            with self.assertRaises(ValueError):
                release.publish(output_dir=self.output, bucket="mskdsp-update", public_base_url=BASE, account_id="b" * 32)
            read.assert_not_called()
            with patch.object(release.subprocess, "run") as run:
                release.upload(self.binary, "p/ci/program", "mskdsp-update", "https://endpoint", "application/gzip", "no-store")
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["AWS_DEFAULT_REGION"], "auto")
            self.assertEqual(env["AWS_REQUEST_CHECKSUM_CALCULATION"], "when_required")
            self.assertEqual(env["AWS_RESPONSE_CHECKSUM_VALIDATION"], "when_required")
            command = run.call_args.args[0]
            self.assertNotIn(SECRETS["AWS_SECRET_ACCESS_KEY"], " ".join(command))

    # 复制凭据时附带的首尾空白不能进入 AWS 请求头。
    def test_secret_copy_whitespace_is_trimmed_before_aws(self):
        secrets = {name: ' \r\n' + value + '\r\n' for name, value in SECRETS.items()}
        with patch.dict(os.environ, secrets, clear=True), patch.object(release.subprocess, "run") as run:
            release.upload(self.binary, "p/ci/program", "mskdsp-update", "https://endpoint", "application/gzip", "no-store")
        environment = run.call_args.kwargs["env"]
        for name, value in SECRETS.items():
            self.assertEqual(environment[name], value)
        self.prepare()
        with patch.dict(os.environ, secrets, clear=True), patch.object(release, "get_json", side_effect=RuntimeError("network reached")):
            with self.assertRaisesRegex(RuntimeError, "network reached"):
                self.publish()

    def test_internal_secret_whitespace_is_rejected_without_leaking_value(self):
        for name in SECRETS:
            with self.subTest(name=name), patch.dict(os.environ, SECRETS | {name: "sensitive\nvalue"}, clear=True), \
                 patch.object(release, "get_json") as read, patch.object(release.subprocess, "run") as run:
                with self.assertRaises(ValueError) as error:
                    self.publish()
                self.assertNotIn("sensitive", str(error.exception))
                read.assert_not_called()
                run.assert_not_called()

    # 默认程序身份检查调用真实 CLI 参数并限制执行时间。
    def test_binary_identity_cli(self):
        identity = {"service": "mskdsp-platform", **{key: self.metadata[key] for key in ("version", "channel", "commit", "build_time")}}
        with patch.object(release.subprocess, "run", return_value=type("Result", (), {"stdout": json.dumps(identity)})) as run:
            self.assertEqual(release.read_binary_identity(self.binary), identity)
        self.assertEqual(run.call_args.args[0][-1], "--version-json")
        self.assertEqual(run.call_args.kwargs["timeout"], 10)


class CleanupTests(unittest.TestCase):
    # 分页全部读完才形成清理计划，并拒绝越界和重复分页 token。
    def test_paginated_listing_and_rejection(self):
        pages = [{"IsTruncated": True, "NextContinuationToken": "next", "Contents": [{"Key": "p/ci/new"}]},
                 {"IsTruncated": False, "Contents": [{"Key": "p/ci/old"}]}]
        with patch.object(release.subprocess, "run", side_effect=[type("Result", (), {"stdout": json.dumps(page)}) for page in pages]) as run:
            self.assertEqual(release.list_channel_objects("bucket", "https://endpoint", "p/ci/"), {"p/ci/new", "p/ci/old"})
            self.assertIn("--continuation-token", run.call_args_list[1].args[0])
        for pages in ([{"IsTruncated": False, "Contents": [{"Key": "p/stable/other"}]}],
                      [{"IsTruncated": True, "NextContinuationToken": "same"}, {"IsTruncated": True, "NextContinuationToken": "same"}]):
            with patch.object(release.subprocess, "run", side_effect=[type("Result", (), {"stdout": json.dumps(page)}) for page in pages]), self.assertRaises(ValueError):
                release.list_channel_objects("bucket", "https://endpoint", "p/ci/")

    # 清理仅逐个删除当前通道旧键，且验证清理后的精确保留集。
    def test_cleanup_exact_keys(self):
        kwargs = dict(bucket="bucket", endpoint="https://endpoint", channel_root="p/ci/", keep_keys={"p/ci/new"}, manifest={"version": "new"}, latest_url=BASE + "/p/ci/latest.json")
        with patch.object(release, "list_channel_objects", side_effect=[{"p/ci/new", "p/ci/old"}, {"p/ci/new"}]), \
             patch.object(release, "get_json", return_value=kwargs["manifest"]), patch.object(release.subprocess, "run") as run:
            self.assertEqual(release.cleanup_old_versions(**kwargs), 1)
            command = run.call_args.args[0]
            self.assertIn("delete-object", command)
            self.assertEqual(command[command.index("--key") + 1], "p/ci/old")
            self.assertNotIn("--recursive", command)

    # 保留集越界、缺失或最新入口变化均禁止删除。
    def test_cleanup_checks_before_deletion(self):
        kwargs = dict(bucket="bucket", endpoint="https://endpoint", channel_root="p/ci/", keep_keys={"p/ci/new"}, manifest={"version": "new"}, latest_url=BASE + "/p/ci/latest.json")
        for keep, listed, latest in (({"p/stable/new"}, {"p/ci/new"}, kwargs["manifest"]), ({"p/ci/new"}, set(), kwargs["manifest"]), ({"p/ci/new"}, {"p/ci/new", "p/ci/old"}, {"version": "other"})):
            with patch.object(release, "list_channel_objects", return_value=listed), patch.object(release, "get_json", return_value=latest), \
                 patch.object(release.subprocess, "run") as run, self.assertRaises(ValueError):
                release.cleanup_old_versions(**(kwargs | {"keep_keys": keep}))
            run.assert_not_called()


class PublicVerificationTests(unittest.TestCase):
    # 公开下载必须同时满足长度和内容摘要，过短、过长与摘要错误都失败。
    def test_public_asset_hash_and_size(self):
        data = b"public artifact"
        sha = hashlib.sha256(data).hexdigest()
        with patch.object(release, "urlopen", return_value=io.BytesIO(data)):
            release.verify_asset(BASE + "/asset", sha, len(data))
        for size, expected_sha in ((len(data) - 1, sha), (len(data) + 1, sha), (len(data), "0" * 64)):
            with self.subTest(size=size), patch.object(release, "urlopen", return_value=io.BytesIO(data)), self.assertRaises(ValueError):
                release.verify_asset(BASE + "/asset", expected_sha, size)

    # 清单仅允许对象且有大小限制，404 可以表示首次发布，其余 HTTP 错误必须中止。
    def test_public_manifest_shape_limit_and_404(self):
        from urllib.error import HTTPError
        for data in (b"[]", b"x" * (release.MAX_MANIFEST + 1)):
            with patch.object(release, "urlopen", return_value=io.BytesIO(data)), self.assertRaises(ValueError):
                release.get_json(BASE + "/latest.json")
        with patch.object(release, "urlopen", side_effect=HTTPError(BASE, 404, "missing", {}, None)):
            self.assertIsNone(release.get_json(BASE, missing_ok=True))
        with patch.object(release, "urlopen", side_effect=HTTPError(BASE, 403, "forbidden", {}, None)), self.assertRaises(HTTPError):
            release.get_json(BASE, missing_ok=True)

    # 发布校验不允许把公开下载重定向到其他地址。
    def test_redirect_rejected(self):
        with self.assertRaises(ValueError):
            release.RejectRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example")


if __name__ == "__main__":
    unittest.main()

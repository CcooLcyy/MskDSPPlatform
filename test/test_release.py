"""发布来源、版本身份和离线镜像包的边界测试。"""
import gzip
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script.release import build_metadata, inspect_archive, validate_source, write_bundle
from script import release

SHA = "0123456789abcdef0123456789abcdef01234567"
TIME = "2026-10-13T08:00:00Z"


class MetadataTests(unittest.TestCase):
    # 确认正式版本身份与 Docker 标签完全一致。
    def test_stable_version(self):
        data = build_metadata("stable", "0.0.1", SHA, TIME, "42.1", "CcooLcyy/MskDSPPlatform")
        self.assertEqual(data["version"], "0.0.1")
        self.assertEqual(data["image"], "ghcr.io/ccoolcyy/mskdsp-platform:0.0.1")
        self.assertEqual(data["release_tag"], "v0.0.1")

    # 确认非正式构建含通道、时间和提交身份，并产生合法 Docker 标签。
    def test_ci_and_beta_versions(self):
        for channel in ("ci", "beta"):
            with self.subTest(channel=channel):
                data = build_metadata(channel, "0.0.1", SHA, TIME, "42.1", "CcooLcyy/MskDSPPlatform")
                self.assertEqual(data["version"], f"0.0.1-{channel}.20261013t080000.42.1+sha.0123456")
                self.assertNotIn("+", data["image_tag"])
                self.assertEqual(data["release_tag"], f"{channel}-latest")

    # 确认重跑编号会生成不同的非正式版本，避免覆盖唯一版本镜像。
    def test_rerun_has_distinct_identity(self):
        a = build_metadata("ci", "0.0.1", SHA, TIME, "42.1", "owner/repo")
        b = build_metadata("ci", "0.0.1", SHA, TIME, "42.2", "owner/repo")
        self.assertNotEqual(a["image"], b["image"])

    # 拒绝非法输入，防止版本与镜像仓库元数据污染。
    def test_invalid_inputs(self):
        valid = ["ci", "0.0.1", SHA, TIME, "42.1", "owner/repo"]
        for index, value in ((0, "prod"), (1, "00.0.1"), (1, "0.0.1-beta"), (2, "bad"), (3, "bad"), (4, "x\ny"), (5, "owner/repo\nx=y")):
            with self.subTest(index=index, value=value):
                args = valid.copy()
                args[index] = value
                with self.assertRaises(ValueError):
                    build_metadata(*args)


class SourceTests(unittest.TestCase):
    # CI 只允许 main，不能通过手动发布参数绕开来源检查。
    def test_ci_main_only(self):
        validate_source("ci", "refs/heads/main", "0.0.1", [])
        for ref in ("beta/0.0.1", "v0.0.1", "feature/test"):
            with self.assertRaises(ValueError):
                validate_source("ci", ref, "0.0.1", [])

    # Beta 分支必须是匹配当前版本的合法数字版本线。
    def test_beta_line(self):
        for ref in ("beta/0.0", "refs/heads/beta/0.0.1"):
            validate_source("beta", ref, "0.0.1", [])
        for ref in ("main", "beta/0.1", "beta/0.0.2", "beta/test", "beta/0.0.1/extra"):
            with self.assertRaises(ValueError):
                validate_source("beta", ref, "0.0.1", [])

    # Stable 标签和匹配的 Beta 提交来源缺一不可。
    def test_stable_lineage(self):
        validate_source("stable", "refs/tags/v0.0.1", "0.0.1", ["origin/beta/0.0.1"])
        for ref, branches in (("v0.0.2", ["origin/beta/0.0.1"]), ("v0.0.1", []), ("v0.0.1", ["origin/beta/1.0"]), ("main", ["origin/beta/0.0.1"])):
            with self.assertRaises(ValueError):
                validate_source("stable", ref, "0.0.1", branches)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metadata = build_metadata("ci", "0.0.1", SHA, TIME, "42.1", "owner/repo")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, tag=None, architecture="amd64", corrupt=False):
        import hashlib
        labels = {"org.opencontainers.image.version": self.metadata["version"], "org.opencontainers.image.revision": SHA, "io.mskdsp.channel": "ci"}
        config = json.dumps({"architecture": architecture, "os": "linux", "config": {"Labels": labels}}).encode()
        name = hashlib.sha256(config).hexdigest() + ".json"
        manifest = json.dumps([{"Config": name, "RepoTags": [tag or self.metadata["image"]], "Layers": ["layer.tar"]}]).encode()
        path = self.root / "image.tar.gz"
        with gzip.open(path, "wb") as zipped, tarfile.open(fileobj=zipped, mode="w") as archive:
            for member_name, content in (("manifest.json", manifest), (name, config + (b" " if corrupt else b"")), ("layer.tar", b"")):
                info = tarfile.TarInfo(member_name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return path

    # 镜像包标签、平台和配置摘要必须与元数据一致。
    def test_archive_identity(self):
        result = inspect_archive(self.archive(), self.metadata)
        self.assertTrue(result.startswith("sha256:"))

    # 错误镜像标签不能伪装成指定版本。
    def test_wrong_tag(self):
        with self.assertRaises(ValueError):
            inspect_archive(self.archive(tag="owner/repo:wrong"), self.metadata)

    # 错误目标架构不能通过发布校验。
    def test_wrong_architecture(self):
        with self.assertRaises(ValueError):
            inspect_archive(self.archive(architecture="arm64"), self.metadata)

    # 配置文件被修改后必须拒绝与原摘要不一致的镜像包。
    def test_corrupt_config_digest(self):
        with self.assertRaises(ValueError):
            inspect_archive(self.archive(corrupt=True), self.metadata)

    # 离线部署清单使用唯一镜像身份，且校验文件覆盖每个交付文件。
    def test_bundle_checksums_and_deploy_identity(self):
        source = self.root / "source"
        (source / "deploy").mkdir(parents=True)
        (source / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (source / "deploy/config.toml").write_text("[server]\n", encoding="utf-8")
        (source / "deploy/run.sh").write_text("docker run example\n", encoding="utf-8")
        (source / "deploy/updater.json").write_text('{"manifest_url":"https://example.com/ci/latest.json"}', encoding="utf-8")
        output = self.root / "bundle"
        with patch.object(release, "inspect_archive", wraps=release.inspect_archive) as inspect, \
             patch.object(release, "sha256_file", wraps=release.sha256_file) as digest:
            write_bundle(self.metadata, self.archive(), output, source)
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(sum(call.args[0].name == "image.tar.gz" for call in digest.call_args_list), 1)
        manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["image"], self.metadata["image"])
        self.assertEqual(manifest["commit"], SHA)
        self.assertIn(self.metadata["image"], (output / ".env").read_text(encoding="utf-8"))
        checksums = (output / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn("latest.json", checksums)
        self.assertIn("image.tar.gz", checksums)
        self.assertTrue((output / "deploy/run.sh").is_file())
        self.assertFalse((output / "compose.yaml").exists())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""生成三通道构建身份，校验 Docker 离线镜像并整理交付文件。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tomllib
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def read_version(root: Path) -> str:
    manifest = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    version = manifest["package"]["version"]
    lock = tomllib.loads((root / "Cargo.lock").read_text(encoding="utf-8"))
    own = [package["version"] for package in lock["package"] if package["name"] == manifest["package"]["name"]]
    if not VERSION_RE.fullmatch(version) or own != [version]:
        raise ValueError("Cargo.toml 与 Cargo.lock 自有包版本必须是相同的正式三段版本")
    return version


def matching_beta(ref: str, base_version: str) -> bool:
    ref = ref.removeprefix("refs/heads/").removeprefix("origin/")
    minor = ".".join(base_version.split(".")[:2])
    return ref in (f"beta/{base_version}", f"beta/{minor}")


def validate_source(channel: str, ref: str, base_version: str, beta_branches: list[str]) -> None:
    ref = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    if channel == "ci":
        if ref != "main":
            raise ValueError("CI 通道只能发布 main 分支")
    elif channel == "beta":
        if not matching_beta(ref, base_version):
            raise ValueError("Beta 通道必须使用与基础版本匹配的 beta/X.Y 或 beta/X.Y.Z 分支")
    elif channel == "stable":
        if ref != f"v{base_version}":
            raise ValueError("正式标签必须与 Cargo 基础版本完全一致")
        if not any(matching_beta(branch, base_version) for branch in beta_branches):
            raise ValueError("正式提交必须属于与版本匹配的远端 Beta 分支")
    else:
        raise ValueError("未知发布通道")


def build_metadata(channel: str, base_version: str, commit: str, timestamp: str, build_id: str, repository: str) -> dict:
    if channel not in ("ci", "beta", "stable") or not VERSION_RE.fullmatch(base_version):
        raise ValueError("发布通道或基础版本不合法")
    if not SHA_RE.fullmatch(commit) or not re.fullmatch(r"\d+\.\d+", build_id):
        raise ValueError("完整提交 SHA 或构建编号不合法")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub 仓库名称不合法")
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("构建时间必须包含时区")
    moment = moment.astimezone(timezone.utc)
    published_at = moment.isoformat(timespec="seconds").replace("+00:00", "Z")
    stamp = moment.strftime("%Y%m%dt%H%M%S")
    version = base_version if channel == "stable" else f"{base_version}-{channel}.{stamp}.{build_id}+sha.{commit[:7]}"
    image_tag = version.replace("+", "-")
    image_repository = "ghcr.io/" + repository.split("/")[0].lower() + "/mskdsp-platform"
    return {
        "base_version": base_version,
        "version": version,
        "channel": channel,
        "commit": commit,
        "build_time": published_at,
        "image_tag": image_tag,
        "image_repository": image_repository,
        "image": f"{image_repository}:{image_tag}",
        "platform": "linux/amd64",
        "release_tag": f"v{base_version}" if channel == "stable" else f"{channel}-latest",
        "artifact_name": f"mskdsp-platform-{image_tag}-linux-amd64",
        "source": f"https://github.com/{repository}",
    }


def sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inspect_archive(path: Path, metadata: dict) -> str:
    # 只读取成员，不解压到文件系统；校验 Docker save 包内真正的镜像身份。
    with tarfile.open(path, "r:gz") as archive:
        manifest_file = archive.extractfile("manifest.json")
        if manifest_file is None:
            raise ValueError("镜像包缺少 Docker manifest.json")
        entries = json.load(manifest_file)
        matches = [entry for entry in entries if metadata["image"] in entry.get("RepoTags", [])]
        if len(matches) != 1:
            raise ValueError("镜像包必须包含唯一的指定版本标签")
        entry = matches[0]
        if not entry.get("Layers") or any(not archive.getmember(layer).isfile() for layer in entry["Layers"]):
            raise ValueError("镜像包缺少完整镜像层")
        config_name = entry["Config"]
        config_file = archive.extractfile(config_name)
        if config_file is None:
            raise ValueError("镜像包缺少配置文件")
        config_bytes = config_file.read()
        digest = hashlib.sha256(config_bytes).hexdigest()
        expected_digest = Path(config_name).name.removesuffix(".json")
        if digest != expected_digest:
            raise ValueError("镜像配置内容与 Docker 配置摘要不一致")
        config = json.loads(config_bytes)
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("镜像目标平台必须是 linux/amd64")
        labels = config.get("config", {}).get("Labels", {})
        for label, key in (("org.opencontainers.image.version", "version"), ("org.opencontainers.image.revision", "commit"), ("io.mskdsp.channel", "channel")):
            if labels.get(label) != metadata[key]:
                raise ValueError(f"镜像标签 {label} 与发布元数据不一致")
        return "sha256:" + digest


def write_bundle(metadata: dict, archive: Path, output: Path, source: Path) -> None:
    if output.exists():
        raise ValueError("交付输出目录已存在，请使用新目录避免混入旧产物")
    image_id = inspect_archive(archive, metadata)
    image_sha256 = sha256_file(archive)
    _write_verified_bundle(metadata, archive, output, source, image_id, image_sha256)


def _write_verified_bundle(metadata: dict, archive: Path, output: Path, source: Path,
                           image_id: str, image_sha256: str) -> None:
    """仅在同一准备进程内复用已校验归档，不接受外部缓存的校验结果。"""
    if output.exists():
        raise ValueError("交付输出目录已存在，请使用新目录避免混入旧产物")
    output.mkdir(parents=True)
    (output / "deploy").mkdir()
    shutil.copyfile(archive, output / "image.tar.gz")
    shutil.copyfile(source / "deploy/config.toml", output / "deploy/config.toml")
    shutil.copyfile(source / "deploy/run.sh", output / "deploy/run.sh")
    updater = {"manifest_url": metadata.get("update_manifest_url") or json.loads((source / "deploy/updater.json").read_text(encoding="utf-8"))["manifest_url"]}
    (output / "deploy/updater.json").write_text(json.dumps(updater, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / ".env").write_text(f"MSKDSP_PLATFORM_IMAGE={metadata['image']}\n", encoding="utf-8")
    manifest = {**metadata, "schema_version": 1, "image_id": image_id, "image_archive": {"file": "image.tar.gz", "sha256": image_sha256}, "bundle_asset": metadata["artifact_name"] + ".zip"}
    (output / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "README.txt").write_text("先校验 SHA256SUMS，再运行：\ndocker load -i image.tar.gz\nsh deploy/run.sh\n打开 http://127.0.0.1:8080 使用网页手动更新；不自动检查或安装。\n默认仅绑定宿主机回环地址；跨机访问请通过受保护的网关。\n持久化卷保存已更新程序；基础镜像和管理器需单独维护。\n", encoding="utf-8")
    checksums = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            name = path.relative_to(output).as_posix()
            sha = image_sha256 if name == "image.tar.gz" else sha256_file(path)
            checksums.append(f"{sha}  {name}")
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(f"离线包校验完成：版本={metadata['version']} 镜像ID={image_id} 目录={output}")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    meta = sub.add_parser("metadata", help="验证发布来源并生成唯一版本身份")
    meta.add_argument("--channel", required=True, choices=("ci", "beta", "stable"))
    meta.add_argument("--ref", required=True)
    meta.add_argument("--repository", required=True)
    meta.add_argument("--build-id", required=True)
    meta.add_argument("--timestamp")
    meta.add_argument("--update-manifest-url")
    meta.add_argument("--output", type=Path, required=True)
    meta.add_argument("--github-output", type=Path)
    bundle = sub.add_parser("bundle", help="校验镜像并生成离线部署目录")
    bundle.add_argument("--metadata", type=Path, required=True)
    bundle.add_argument("--archive", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--source", type=Path, default=Path("."))
    args = parser.parse_args()
    if args.command == "bundle":
        write_bundle(json.loads(args.metadata.read_text(encoding="utf-8")), args.archive, args.output, args.source)
        return
    version = read_version(Path("."))
    commit = git("rev-parse", "HEAD")
    branches = git("branch", "-r", "--contains", commit, "--format=%(refname:short)").splitlines()
    validate_source(args.channel, args.ref, version, branches)
    ref = args.ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    target = f"refs/tags/{ref}^{{commit}}" if args.channel == "stable" else f"refs/remotes/origin/{ref}"
    if git("rev-parse", target) != commit:
        raise ValueError("检出的提交与发布来源最新提交不一致")
    timestamp = args.timestamp or (git("show", "-s", "--format=%cI", "HEAD") if args.channel == "stable" else datetime.now(timezone.utc).isoformat())
    data = build_metadata(args.channel, version, commit, timestamp, args.build_id, args.repository)
    data["ref"] = ref
    if args.update_manifest_url:
        source = urlsplit(args.update_manifest_url)
        if source.scheme != "https" or not source.hostname or source.username or source.password or source.query or source.fragment or source.path.split("/")[-2:] != [args.channel, "latest.json"]:
            raise ValueError("网页更新源必须是对应通道的 HTTPS latest.json")
        data["update_manifest_url"] = args.update_manifest_url
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in data.items():
                output.write(f"{key}={value}\n")
    print(f"发布目标：通道={data['channel']} 版本={data['version']} 提交={commit} 镜像={data['image']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"发布校验失败：{error}") from error

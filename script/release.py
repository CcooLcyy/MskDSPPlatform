#!/usr/bin/env python3
"""生成 CI/Stable 构建身份，校验 Docker 离线镜像并整理交付文件。"""
from __future__ import annotations

import argparse
import base64
from contextlib import nullcontext
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))

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


def validate_source(channel: str, ref: str, base_version: str, containing_branches: list[str]) -> None:
    if not VERSION_RE.fullmatch(base_version):
        raise ValueError("基础版本必须是无前导零的三段版本")
    ref = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    if channel == "ci":
        if ref != "main":
            raise ValueError("CI 通道只能发布 main 分支")
    elif channel == "stable":
        if ref != f"v{base_version}":
            raise ValueError("正式标签必须与 Cargo 基础版本完全一致")
        allowed = {"origin/main", "origin/release/" + ".".join(base_version.split(".")[:2])}
        if not allowed.intersection(containing_branches):
            raise ValueError("正式提交必须属于远端 main 或匹配版本 release/X.Y 的历史")
    else:
        raise ValueError("仅允许 CI/Stable 新发布，Beta 已退役")


def build_metadata(channel: str, base_version: str, commit: str, timestamp: str, build_id: str, repository: str) -> dict:
    if channel not in ("ci", "stable") or not VERSION_RE.fullmatch(base_version):
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
    stamp = moment.astimezone(BEIJING).strftime("%Y%m%dt%H%M%S")
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


def inspect_archive(path: Path | tarfile.TarFile, metadata: dict) -> str:
    # 只读取成员，不解压到文件系统；校验 Docker save 包内真正的镜像身份。
    context = nullcontext(path) if isinstance(path, tarfile.TarFile) else tarfile.open(path, "r:*")
    with context as archive:
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
    if metadata.get("channel") not in ("ci", "stable"):
        raise ValueError("仅允许生成 CI/Stable 交付包，Beta 已退役")
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
    (output / "deploy/network").mkdir()
    for name in ("grpc_network_host.py", "install_grpc_network.py"):
        shutil.copyfile(source / "script" / name, output / "deploy/network" / name)
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


def git_environment() -> dict:
    """为临时仓库继承短期只读凭据，不写入 Git 配置、远端 URL 或日志。"""
    environment = os.environ.copy()
    token = environment.pop("PLATFORM_SOURCE_READ_TOKEN", "")
    if token:
        if not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("来源仓库临时凭据格式无效")
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
        if not 0 <= count <= 100:
            raise ValueError("Git 临时配置数量不合法")
        authorization = base64.b64encode(("x-access-token:" + token).encode("ascii")).decode("ascii")
        environment["GIT_CONFIG_COUNT"] = str(count + 1)
        environment[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraHeader"
        environment[f"GIT_CONFIG_VALUE_{count}"] = "AUTHORIZATION: basic " + authorization
    return environment


def git(*args: str, cwd=None) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, env=git_environment(), text=True, encoding="utf-8").strip()


def verify_remote_stable_source(ref: str, version: str, commit: str, *, repo=Path(".")) -> None:
    """查询并抓取当前远端来源，不依赖工作区可能过期的跟踪引用。"""
    tag = ref.removeprefix("refs/tags/")
    if not VERSION_RE.fullmatch(version) or tag != "v" + version or not SHA_RE.fullmatch(commit):
        raise ValueError("正式标签、版本或冻结提交不合法")
    tag_ref = "refs/tags/" + tag
    allowed = ("refs/heads/main", "refs/heads/release/" + ".".join(version.split(".")[:2]))
    refs = (tag_ref, tag_ref + "^{}", *allowed)
    def snapshot():
        result = {}
        for line in git("ls-remote", "origin", *refs, cwd=repo).splitlines():
            sha, name = line.split()
            if name not in refs or not SHA_RE.fullmatch(sha) or name in result:
                raise ValueError("远端来源引用不合法")
            result[name] = sha
        if tag_ref not in result or result.get(tag_ref + "^{}", result[tag_ref]) != commit:
            raise ValueError("远端正式标签缺失或已移动，拒绝发布冻结产物")
        return result
    observed = snapshot()
    remote = git("remote", "get-url", "origin", cwd=repo)
    containing = []
    # 临时仓库只保留本次观察到的完整历史，不改变用户分支或检出状态。
    with tempfile.TemporaryDirectory(prefix="platform-stable-source-") as temporary:
        git("init", "--bare", temporary)
        for branch in allowed:
            if branch not in observed:
                continue
            git("fetch", "--no-tags", remote, branch + ":" + branch, cwd=temporary)
            if git("rev-parse", branch, cwd=temporary) != observed[branch]:
                raise ValueError("校验期间远端分支发生变化，请重新发布")
            ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", commit, branch],
                                      cwd=temporary, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ancestry.returncode == 0:
                containing.append("origin/" + branch.removeprefix("refs/heads/"))
    if snapshot() != observed:
        raise ValueError("校验期间远端引用发生变化，请重新发布")
    validate_source("stable", tag, version, containing)
    print(f"正式发布来源校验通过：标签={tag} 提交={commit} 来源={','.join(containing)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    meta = sub.add_parser("metadata", help="验证发布来源并生成唯一版本身份")
    meta.add_argument("--channel", required=True, choices=("ci", "stable"))
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
    verify = sub.add_parser("verify-source", help="对外发布前重新校验冻结 Stable 来源")
    verify.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify-source":
        frozen = json.loads(args.metadata.read_text(encoding="utf-8"))
        if frozen.get("channel") != "stable" or frozen.get("version") != frozen.get("base_version"):
            raise ValueError("仅允许校验冻结的 Stable 发布身份")
        verify_remote_stable_source(frozen["ref"], frozen["base_version"], frozen["commit"])
        return
    if args.command == "bundle":
        write_bundle(json.loads(args.metadata.read_text(encoding="utf-8")), args.archive, args.output, args.source)
        return
    version = read_version(Path("."))
    commit = git("rev-parse", "HEAD")
    ref = args.ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    if args.channel == "stable":
        verify_remote_stable_source(args.ref, version, commit)
    else:
        validate_source(args.channel, args.ref, version, [])
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

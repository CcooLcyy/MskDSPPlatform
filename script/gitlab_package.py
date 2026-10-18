#!/usr/bin/env python3
"""无需 Docker daemon，校验 BuildKit Docker 归档并准备既有 R2/离线交付产物。"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import publish_r2
from script.release import inspect_archive, sha256_file

TARGET = "usr/local/bin/mskdsp-platform"
ANCESTORS = ("usr", "usr/local", "usr/local/bin")


def safe_name(name: str) -> str:
    """归档成员仅在内存中寻址，始终不将其路径用于文件系统写入。"""
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or ".." in path.parts or "\0" in name:
        raise ValueError("镜像归档含不安全的成员路径")
    return path.as_posix()


def read_member(archive: tarfile.TarFile, members: dict, name: str, limit: int) -> bytes:
    member = members.get(safe_name(name))
    if member is None or not member.isfile() or not 0 <= member.size <= limit:
        raise ValueError("镜像归档成员缺失、类型或大小不合法")
    with archive.extractfile(member) as source:
        data = source.read(limit + 1)
    if len(data) != member.size:
        raise ValueError("镜像归档成员长度不完整")
    return data


def extract_binary(image_tar: Path, metadata: dict, destination: Path) -> None:
    """顺序叠加目标文件及祖先目录，whiteout 只删除前层内容。"""
    binary = None
    kinds = {}
    with tarfile.open(image_tar, "r:") as archive:
        members = {}
        for member in archive:
            name = safe_name(member.name)
            if name in members:
                raise ValueError("镜像外层归档含重复成员")
            members[name] = member
        manifest = json.loads(read_member(archive, members, "manifest.json", publish_r2.MAX_MANIFEST))
        entries = [entry for entry in manifest if metadata["image"] in entry.get("RepoTags", [])]
        if len(entries) != 1:
            raise ValueError("镜像包必须包含唯一的指定版本标签")
        entry = entries[0]
        config = json.loads(read_member(archive, members, entry["Config"], publish_r2.MAX_MANIFEST))
        layers = entry["Layers"]
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        if diff_ids is not None and len(diff_ids) != len(layers):
            raise ValueError("镜像层数量与配置摘要不一致")
        for index, layer in enumerate(layers):
            member = members.get(safe_name(layer))
            if member is None or not member.isfile():
                raise ValueError("镜像包缺少普通文件类型的镜像层")
            if diff_ids is not None:
                with archive.extractfile(member) as source:
                    compressed = source.read(2) == b"\x1f\x8b"
                    source.seek(0)
                    if compressed:
                        # BuildKit 的 Docker 导出也可能保留 gzip 层；diff_id 对应解压后的 tar。
                        with gzip.GzipFile(fileobj=source) as uncompressed:
                            digest = "sha256:" + hashlib.file_digest(uncompressed, "sha256").hexdigest()
                    else:
                        digest = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
                if digest != diff_ids[index]:
                    raise ValueError("镜像层内容与配置摘要不一致")
            # 先登记本层变更，再执行删除，避免归档顺序改变 whiteout 语义。
            changes, deletions = [], []
            with archive.extractfile(member) as source, tarfile.open(fileobj=source, mode="r|*") as layer_tar:
                for item in layer_tar:
                    name = safe_name(item.name)
                    path = PurePosixPath(name)
                    if path.name == ".wh..wh..opq":
                        deletions.append((path.parent.as_posix(), True))
                    elif path.name.startswith(".wh."):
                        deletions.append(((path.parent / path.name[4:]).as_posix(), False))
                    elif name in (*ANCESTORS, TARGET):
                        content = None
                        if name == TARGET and item.isfile():
                            if not 0 < item.size <= publish_r2.MAX_BINARY:
                                raise ValueError("程序大小必须在 128 MiB 以内且非空")
                            with layer_tar.extractfile(item) as executable:
                                content = executable.read(publish_r2.MAX_BINARY + 1)
                            if len(content) != item.size:
                                raise ValueError("镜像中的程序内容不完整")
                        changes.append((name, "directory" if item.isdir() else "file" if item.isfile() else "link", content))
            for name, opaque in deletions:
                prefix = "" if name == "." else name + "/"
                if (opaque and TARGET.startswith(prefix)) or (not opaque and (TARGET == name or TARGET.startswith(prefix))):
                    binary = None
                kinds = {key: kind for key, kind in kinds.items()
                         if not (key.startswith(prefix) or (not opaque and key == name))}
            for name, kind, content in changes:
                kinds[name] = kind
                if name == TARGET:
                    binary = content
                elif kind != "directory":
                    binary = None
    if binary is None or kinds.get(TARGET) != "file":
        raise ValueError("最终镜像缺少普通文件类型的 mskdsp-platform 程序")
    if any(kinds.get(name, "directory") != "directory" for name in ANCESTORS):
        raise ValueError("程序的祖先路径不是普通目录，拒绝跟随镜像链接")
    destination.write_bytes(binary)
    destination.chmod(0o755)


def prepare(*, metadata: Path, image_tar: Path, public_base_url: str, prefix: str = "mskdsp-platform",
            package_dir: Path = Path("package"), r2_output: Path = Path("r2"),
            bundle_output: Path = Path("bundle"), release_output: Path = Path("release"),
            bundle_source: Path = Path(".")) -> dict:
    data = json.loads(metadata.read_text(encoding="utf-8"))
    publish_r2.validate_metadata(data)
    publish_r2.public_url(public_base_url)
    publish_r2.valid_prefix(prefix)
    if not image_tar.is_file() or image_tar.is_symlink():
        raise ValueError("缺少 BuildKit 导出的普通 Docker 镜像归档")
    if any(path.exists() for path in (r2_output, bundle_output, release_output)):
        raise ValueError("交付输出目录已存在，请使用新目录避免混入旧产物")
    package_dir.mkdir(parents=True, exist_ok=True)
    compressed, binary = package_dir / "image.tar.gz", package_dir / "mskdsp-platform"
    if any(path.exists() for path in (compressed, binary)) or image_tar.resolve() == compressed.resolve():
        raise ValueError("程序或压缩镜像输出已存在，拒绝覆盖")
    with image_tar.open("rb") as source, compressed.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
            shutil.copyfileobj(source, zipped)
    # 校验平台、标签、配置摘要之后才允许提取和执行程序身份命令。
    inspect_archive(compressed, data)
    extract_binary(image_tar, data, binary)
    manifest = publish_r2.prepare(metadata=metadata, image_archive=compressed, binary=binary,
                                  output=r2_output, public_base_url=public_base_url, prefix=prefix,
                                  bundle_output=bundle_output, bundle_source=bundle_source)
    release_output.mkdir(parents=True)
    shutil.make_archive(str(release_output / data["artifact_name"]), "zip", bundle_output)
    shutil.copyfile(bundle_output / "latest.json", release_output / "latest.json")
    shutil.copyfile(r2_output / "latest.json", release_output / "updater-manifest.json")
    for asset in (r2_output / "assets").rglob("*-binary.tar.gz"):
        shutil.copyfile(asset, release_output / asset.name)
    sums = [f"{sha256_file(path)}  {path.name}" for path in sorted(release_output.iterdir()) if path.is_file()]
    (release_output / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print(f"GitLab 交付准备完成：版本={data['version']} 目录={release_output}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare", help="从 Docker 归档准备程序、R2 与离线交付包")
    command.add_argument("--metadata", type=Path, required=True)
    command.add_argument("--image-tar", type=Path, required=True)
    command.add_argument("--public-base-url", required=True)
    command.add_argument("--prefix", default="mskdsp-platform")
    for option, default in (("package-dir", "package"), ("r2-output", "r2"), ("bundle-output", "bundle"),
                            ("release-output", "release"), ("bundle-source", ".")):
        command.add_argument("--" + option, type=Path, default=Path(default))
    args = vars(parser.parse_args())
    args.pop("command")
    prepare(**args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        raise SystemExit(f"GitLab 交付准备失败：{error}") from error

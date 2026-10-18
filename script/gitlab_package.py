#!/usr/bin/env python3
"""无需 Docker daemon，校验 BuildKit Docker 归档并准备既有 R2/离线交付产物。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script import publish_r2
from script.release import _write_verified_bundle, inspect_archive, sha256_file

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


class DigestReader:
    """在 tar 解析消费同一解压流时计算层摘要，避免第二次读取/解压。"""
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        content = self.source.read(size)
        self.digest.update(content)
        return content


class DigestWriter:
    def __init__(self, destination):
        self.destination = destination
        self.digest = hashlib.sha256()

    def write(self, content):
        result = self.destination.write(content)
        self.digest.update(content[:result])
        return result

    def flush(self):
        self.destination.flush()


def extract_binary(image_tar: Path, metadata: dict, destination: Path) -> str:
    """顺序叠加目标文件及祖先目录，whiteout 只删除前层内容。"""
    binary = None
    kinds = {}
    with tarfile.open(image_tar, "r:") as archive:
        image_id = inspect_archive(archive, metadata)
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
            # 先登记本层变更，再执行删除，避免归档顺序改变 whiteout 语义。
            changes, deletions = [], []
            with ExitStack() as stack:
                source = stack.enter_context(archive.extractfile(member))
                compressed = source.read(2) == b"\x1f\x8b"
                source.seek(0)
                uncompressed = stack.enter_context(gzip.GzipFile(fileobj=source)) if compressed else source
                reader = DigestReader(uncompressed)
                with tarfile.open(fileobj=reader, mode="r|") as layer_tar:
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
                # tar EOF 后的 padding 也属于 diff_id，必须消费全部解压流。
                while reader.read(1024 * 1024):
                    pass
                if diff_ids is not None and "sha256:" + reader.digest.hexdigest() != diff_ids[index]:
                    raise ValueError("镜像层内容与配置摘要不一致")
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
    return image_id


def prepare(*, metadata: Path, image_tar: Path, public_base_url: str, prefix: str = "mskdsp-platform",
            package_dir: Path = Path("package"), r2_output: Path = Path("r2"),
            bundle_output: Path = Path("bundle"), release_output: Path = Path("release"),
            bundle_source: Path = Path("."), skip_offline: bool = False) -> dict:
    data = json.loads(metadata.read_text(encoding="utf-8"))
    publish_r2.validate_metadata(data)
    publish_r2.public_url(public_base_url)
    publish_r2.valid_prefix(prefix)
    if not image_tar.is_file() or image_tar.is_symlink():
        raise ValueError("缺少 BuildKit 导出的普通 Docker 镜像归档")
    outputs = (r2_output,) if skip_offline else (r2_output, bundle_output, release_output)
    if any(path.exists() for path in outputs):
        raise ValueError("交付输出目录已存在，请使用新目录避免混入旧产物")
    package_dir.mkdir(parents=True, exist_ok=True)
    compressed, binary = package_dir / "image.tar.gz", package_dir / "mskdsp-platform"
    if any(path.exists() for path in (compressed, binary)) or image_tar.resolve() == compressed.resolve():
        raise ValueError("程序或压缩镜像输出已存在，拒绝覆盖")
    # 原始 tar 只校验一次，执行程序前完成身份、层摘要和路径语义验证。
    image_id = extract_binary(image_tar, data, binary)
    with image_tar.open("rb") as source, compressed.open("wb") as raw:
        writer = DigestWriter(raw)
        with gzip.GzipFile(filename="", fileobj=writer, mode="wb", mtime=0) as zipped:
            shutil.copyfileobj(source, zipped)
    manifest = publish_r2._prepare_verified(data=data, image_archive=compressed, binary=binary,
                                           output=r2_output, public_base_url=public_base_url, prefix=prefix,
                                           image_id=image_id, image_sha256=writer.digest.hexdigest(),
                                           bundle_output=None if skip_offline else bundle_output, bundle_source=bundle_source)
    if not skip_offline:
        write_release(data, r2_output, bundle_output, release_output)
    print(f"GitLab 交付准备完成：版本={data['version']} 目录={r2_output if skip_offline else release_output}")
    return manifest


def write_release(data: dict, r2_output: Path, bundle_output: Path, release_output: Path) -> None:
    release_output.mkdir(parents=True)
    with zipfile.ZipFile(release_output / (data["artifact_name"] + ".zip"), "w") as archive:
        for path in sorted(bundle_output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle_output).as_posix(),
                              compress_type=zipfile.ZIP_STORED if path.name.endswith(".tar.gz") else zipfile.ZIP_DEFLATED)
    shutil.copyfile(bundle_output / "latest.json", release_output / "latest.json")
    shutil.copyfile(r2_output / "latest.json", release_output / "updater-manifest.json")
    tag = data["version"].replace("+", "-")
    asset = r2_output / "assets" / tag / publish_r2.PLATFORM / f"{publish_r2.PROJECT}-{tag}-{publish_r2.PLATFORM}-binary.tar.gz"
    shutil.copyfile(asset, release_output / asset.name)
    sums = [f"{sha256_file(path)}  {path.name}" for path in sorted(release_output.iterdir()) if path.is_file()]
    (release_output / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")


def offline_bundle(*, metadata: Path, output_dir: Path, public_base_url: str, prefix: str = "mskdsp-platform",
                   bundle_output: Path = Path("bundle"), release_output: Path = Path("release"),
                   bundle_source: Path = Path(".")) -> dict:
    """从跨任务下载的 R2 产物重新校验，再按需组装离线交付，无需原始镜像 tar。"""
    if any(path.exists() for path in (bundle_output, release_output)):
        raise ValueError("交付输出目录已存在，请使用新目录避免混入旧产物")
    data = json.loads(metadata.read_text(encoding="utf-8"))
    publish_r2.validate_metadata(data)
    manifest, _, assets = publish_r2.validate_local_assets(output_dir=output_dir, public_base_url=public_base_url, prefix=prefix)
    if json.loads((output_dir / "latest.json").read_text(encoding="utf-8")) != manifest:
        raise ValueError("R2 更新入口与发布清单不一致")
    if any(manifest[key] != data[key] for key in ("version", "channel", "commit", "base_version", "build_time")):
        raise ValueError("离线交付元数据与 R2 产物不一致")
    image_archive = assets[0][0]
    image_id = inspect_archive(image_archive, data)
    entry = manifest["platforms"][publish_r2.PLATFORM]
    if image_id != entry["image_id"] or data["image"] != entry["image"]:
        raise ValueError("离线镜像身份与 R2 清单不一致")
    _write_verified_bundle(data, image_archive, bundle_output, bundle_source, image_id, entry["sha256"])
    write_release(data, output_dir, bundle_output, release_output)
    print(f"离线交付准备完成：版本={data['version']} 目录={release_output}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare", help="从 Docker 归档准备程序、R2 与离线交付包")
    command.add_argument("--metadata", type=Path, required=True)
    command.add_argument("--image-tar", type=Path, required=True)
    command.add_argument("--public-base-url", required=True)
    command.add_argument("--prefix", default="mskdsp-platform")
    command.add_argument("--skip-offline", action="store_true", help="只准备 R2 产物，不创建离线 bundle/ZIP")
    for option, default in (("package-dir", "package"), ("r2-output", "r2"), ("bundle-output", "bundle"),
                            ("release-output", "release"), ("bundle-source", ".")):
        command.add_argument("--" + option, type=Path, default=Path(default))
    offline = commands.add_parser("offline-bundle", help="从已准备的 R2 产物重新校验并创建离线交付")
    offline.add_argument("--metadata", type=Path, required=True)
    offline.add_argument("--output-dir", type=Path, default=Path("r2"))
    offline.add_argument("--public-base-url", required=True)
    offline.add_argument("--prefix", default="mskdsp-platform")
    for option, default in (("bundle-output", "bundle"), ("release-output", "release"), ("bundle-source", ".")):
        offline.add_argument("--" + option, type=Path, default=Path(default))
    args = vars(parser.parse_args())
    selected = args.pop("command")
    (prepare if selected == "prepare" else offline_bundle)(**args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        raise SystemExit(f"GitLab 交付准备失败：{error}") from error

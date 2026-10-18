#!/usr/bin/env python3
"""只读选择软件仓库中已完成入库的平台 CI 包，不依赖平台 HTTP。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

import update_common as common


def reject_links(path):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise common.UpdateError("本地软件仓库路径不能包含链接：" + str(part))
    return path


class LocalSoftwareSource:
    def __init__(self, root, channel="ci"):
        if channel != "ci":
            raise common.UpdateError("平台本地更新当前仅支持 CI 通道")
        self.root, self.channel = Path(root), channel

    def package_path(self, metadata):
        # 不信任数据库 JSON，所有路径片段在拼接前单独检查。
        for key in ("product", "version", "platform", "file_name"):
            value = metadata.get(key)
            if (not isinstance(value, str) or value in (".", "..")
                    or not re.fullmatch(r"[A-Za-z0-9_.+-]+", value)):
                raise common.UpdateError("本地包路径字段无效：" + key)
        path = self.root / "packages" / metadata["product"] / metadata["version"] / metadata["platform"] / metadata["file_name"]
        reject_links(path)
        if not path.is_file():
            raise common.UpdateError("已入库平台程序包缺失")
        return path

    def validate(self, value, indexed):
        if (not isinstance(value, dict) or value.get("product") != common.PROJECT
                or value.get("platform") != "linux-amd64" or value.get("channel") != self.channel
                or value.get("source_ref") != "main"):
            raise common.UpdateError("本地平台包产品、架构、通道或来源分支不正确")
        if tuple(value.get(key) for key in ("product", "version", "platform")) != indexed:
            raise common.UpdateError("本地包元数据与数据库索引不一致")
        common.validate_commit(self.channel, value.get("version"), value.get("source_sha"))
        common.validate_time(value.get("build_time"))
        if type(value.get("size")) is not int or not 0 < value["size"] <= common.MAX_BINARY:
            raise common.UpdateError("本地包大小无效或超过限制")
        if not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            raise common.UpdateError("本地包 SHA256 无效")
        if not isinstance(value.get("file_name"), str) or not value["file_name"].endswith(".tar.gz"):
            raise common.UpdateError("本地平台包必须是 tar.gz 程序包")
        path = self.package_path(value)
        artifact = dict(binary_size=value["size"], binary_sha256=value["sha256"], local_path=str(path))
        manifest = dict(schema_version=1, project=common.PROJECT, channel=self.channel,
                        version=value["version"], commit=value["source_sha"], build_time=value["build_time"],
                        published_at=value["build_time"], platforms={"linux-amd64": artifact})
        return manifest, artifact

    def latest(self):
        database = reject_links(self.root / "software.sqlite3")
        if not database.exists():
            return None
        for suffix in ("-wal", "-shm", "-journal"):
            reject_links(Path(str(database) + suffix))
        if not database.is_file():
            raise common.UpdateError("软件仓库数据库不是普通文件")
        try:
            # packages 仅由 CompleteUpload 成功事务写入；不读取 uploads 或发布时间。
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                db.execute("PRAGMA query_only=ON")
                rows = db.execute("SELECT metadata,product,version,platform FROM packages WHERE product=? AND platform=?",
                                  (common.PROJECT, "linux-amd64"))
                candidates = []
                for raw, product, version, platform in rows:
                    if len(raw) > common.MAX_MANIFEST:
                        raise common.UpdateError("本地包元数据超过大小限制")
                    value = json.loads(raw)
                    # 历史其他通道的包保留，但不能成为 CI 候选。
                    if isinstance(value, dict) and value.get("channel") != self.channel:
                        continue
                    candidates.append(self.validate(value, (product, version, platform)))
            return max(candidates, key=lambda item: common.version_order(self.channel, item[0]["version"]), default=None)
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            if isinstance(exc, common.UpdateError):
                raise
            raise common.UpdateError("读取本地平台软件仓库失败：" + str(exc)) from exc

    def copy_artifact(self, artifact, folder):
        source = Path(artifact["local_path"])
        # 再次约束调用时路径，防止候选检查后被替换为链接。
        root = self.root.absolute()
        if not source.absolute().is_relative_to(root / "packages"):
            raise common.UpdateError("本地程序包不在软件仓库内")
        reject_links(source)
        descriptor, name = tempfile.mkstemp(prefix="local-", suffix=".tar.gz", dir=folder)
        destination = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                with os.fdopen(os.open(source, flags), "rb") as incoming:
                    if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                        raise common.UpdateError("本地程序包必须是普通文件")
                    count, digest = 0, hashlib.sha256()
                    while chunk := incoming.read(min(1024 * 1024, artifact["binary_size"] - count + 1)):
                        count += len(chunk)
                        if count > artifact["binary_size"]:
                            raise common.UpdateError("本地程序包超过声明大小")
                        digest.update(chunk)
                        target.write(chunk)
                    if count != artifact["binary_size"] or digest.hexdigest() != artifact["binary_sha256"]:
                        raise common.UpdateError("本地程序包大小或 SHA256 不一致")
                target.flush()
                os.fsync(target.fileno())
            return destination
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise common.UpdateError("复制校验本地程序包失败：" + str(exc)) from exc

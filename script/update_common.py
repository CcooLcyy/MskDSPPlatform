#!/usr/bin/env python3
"""平台更新共用校验、受限下载和持久化工具，仅使用 Python 标准库。"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROJECT = "mskdsp-platform"
MAX_MANIFEST = 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024
SEMVER = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
try:
    import fcntl
except ImportError:
    fcntl = None


class UpdateError(ValueError):
    pass


def version_order(channel, version):
    if not isinstance(version, str):
        raise UpdateError("版本必须是字符串")
    if channel == "stable":
        found = re.fullmatch(SEMVER, version)
        if found:
            return tuple(map(int, found.groups()))
    elif channel in ("ci", "beta"):
        found = re.fullmatch(SEMVER + rf"-{channel}\.([0-9]{{8}}t[0-9]{{6}})\.([0-9]+)\.([0-9]+)\+sha\.([0-9a-f]{{7}})", version)
        if found:
            try:
                datetime.strptime(found[4], "%Y%m%dt%H%M%S")
            except ValueError as exc:
                raise UpdateError("版本包含无效 UTC 时间") from exc
            return tuple(map(int, found.groups()[:3])) + (int(found[4].replace("t", "")), int(found[5]), int(found[6]))
    raise UpdateError("版本格式与通道不一致")


def validate_commit(channel, version, commit):
    version_order(channel, version)
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpdateError("来源提交必须是 40 位小写 SHA")
    if channel != "stable" and version.rsplit("+sha.", 1)[-1] != commit[:7]:
        raise UpdateError("短提交号与完整提交不一致")


def validate_time(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("缺少时区")
    except (ValueError, AttributeError) as exc:
        raise UpdateError("发布时间必须是包含时区的 ISO8601 时间") from exc


class Transport:
    def __init__(self, *, allow_loopback_http=False):
        # 仅测试显式启用；CLI/环境变量没有放开明文 HTTP 的开关。
        self.allow_loopback_http = allow_loopback_http

    def validate_url(self, value, reference=None):
        if not isinstance(value, str):
            raise UpdateError("下载地址必须是字符串")
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise UpdateError("下载地址不能包含空白或控制字符")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            loopback = False
            try:
                loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                pass
            allowed_http = self.allow_loopback_http and loopback and parsed.scheme == "http"
            if (parsed.scheme != "https" and not allowed_http) or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise UpdateError("下载地址必须是无凭据 HTTPS 绝对地址")
            if parsed.query or parsed.fragment or "\\" in parsed.path or unquote(parsed.path) != parsed.path or any(part in (".", "..") for part in parsed.path.split("/")):
                raise UpdateError("下载地址包含禁止的编码、参数或路径")
            if reference:
                base = self.validate_url(reference)
                prefix = base.path.rsplit("/", 1)[0] + "/"
                if (parsed.scheme, parsed.netloc.lower()) != (base.scheme, base.netloc.lower()) or not parsed.path.startswith(prefix):
                    raise UpdateError("下载地址必须位于清单同源通道目录内")
            return parsed
        except ValueError as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("下载地址不合法") from exc

    def open(self, url, reference=None):
        self.validate_url(url, reference)
        transport = self
        boundary = reference or url
        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, message, headers, newurl):
                transport.validate_url(newurl, boundary)
                return super().redirect_request(request, fp, code, message, headers, newurl)
        return build_opener(Redirect()).open(Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "mskdsp-platform-updater/1"}), timeout=20)

    def fetch_manifest(self, url):
        try:
            with self.open(url) as response:
                raw = response.read(MAX_MANIFEST + 1)
            if len(raw) > MAX_MANIFEST:
                raise UpdateError("清单超过大小限制")
            return json.loads(raw)
        except (OSError, ValueError) as exc:
            raise UpdateError("读取更新清单失败：" + str(exc)) from exc


def platform_key():
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise UpdateError("平台更新当前仅支持 Linux amd64")
    return "linux-amd64"


def validate_manifest(data, url, selected_platform="linux-amd64", transport=None):
    transport = transport or Transport()
    base = transport.validate_url(url)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1 or data.get("project") != PROJECT:
        raise UpdateError("清单版本或项目不正确")
    channel = data.get("channel")
    if channel not in ("ci", "beta", "stable") or base.path.split("/")[-2:] != [channel, "latest.json"]:
        raise UpdateError("清单地址与通道不一致")
    validate_commit(channel, data.get("version"), data.get("commit"))
    validate_time(data.get("published_at"))
    validate_time(data.get("build_time", data.get("published_at")))
    entries = data.get("platforms")
    if not isinstance(entries, dict) or selected_platform not in entries:
        raise UpdateError("清单缺少本机架构")
    for key, entry in entries.items():
        if key != "linux-amd64" or not isinstance(entry, dict):
            raise UpdateError("清单架构不受支持")
        transport.validate_url(entry.get("binary_url"), url)
        if type(entry.get("binary_size")) is not int or not 0 < entry["binary_size"] <= MAX_BINARY:
            raise UpdateError("二进制包大小无效或超过限制")
        if not isinstance(entry.get("binary_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["binary_sha256"]):
            raise UpdateError("二进制包 SHA256 无效")
    return entries[selected_platform]


def needs_update(manifest, state):
    if not state or state.get("channel") == "dev":
        return True
    if manifest["channel"] != state["channel"]:
        raise UpdateError("禁止自动切换发布通道")
    new, old = (version_order(item["channel"], item["version"]) for item in (manifest, state))
    if new < old:
        raise UpdateError("远端版本低于已安装版本，拒绝回退")
    if new == old:
        for key in ("version", "commit", "build_time"):
            expected = manifest.get(key, manifest.get("published_at"))
            if state.get(key) != expected:
                raise UpdateError("同版本构建身份发生变化，拒绝覆盖")
        entry = manifest["platforms"].get(state.get("platform", "linux-amd64"), {})
        if state.get("binary_sha256") and state["binary_sha256"] != entry.get("binary_sha256"):
            raise UpdateError("同版本二进制摘要发生变化，拒绝覆盖")
        return False
    return True


def download_artifact(entry, folder, manifest_url, transport=None):
    transport = transport or Transport()
    descriptor, name = tempfile.mkstemp(prefix="download-", suffix=".tar.gz", dir=folder)
    path = Path(name)
    digest, count = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "wb") as target, transport.open(entry["binary_url"], manifest_url) as response:
            while True:
                chunk = response.read(min(1024 * 1024, entry["binary_size"] - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > entry["binary_size"]:
                    raise UpdateError("下载包超过清单声明大小")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if count != entry["binary_size"] or digest.hexdigest() != entry["binary_sha256"]:
            raise UpdateError("下载包大小或 SHA256 不一致")
        return path
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise UpdateError("下载二进制包失败：" + str(exc)) from exc


def extract_binary(archive_path, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            count = 0
            for member in archive:
                count += 1
                if (count != 1 or member.name != PROJECT or not member.isreg()
                        or member.sparse is not None or not 0 < member.size <= MAX_BINARY):
                    raise UpdateError("归档必须仅包含根目录的一个普通平台二进制")
                with archive.extractfile(member) as source, (destination / PROJECT).open("xb") as target:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise UpdateError("归档内容不完整")
                        target.write(chunk)
                        remaining -= len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            if count != 1:
                raise UpdateError("归档缺少平台二进制")
        (destination / PROJECT).chmod(0o755)
        return destination / PROJECT
    except (OSError, tarfile.TarError) as exc:
        raise UpdateError("读取二进制归档失败：" + str(exc)) from exc


def verify_elf(path, key="linux-amd64"):
    with Path(path).open("rb") as source:
        header = source.read(64)
    if key != "linux-amd64" or len(header) != 64 or header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise UpdateError("候选程序必须是 amd64 小端 64 位 ELF")


def atomic_json(path, value):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                try:
                    os.fsync(descriptor)
                except OSError as exc:
                    # rename 已提交；目录刷盘失败不能视为未提交并执行回滚。
                    print("更新状态已提交，目录刷盘失败：" + str(exc), flush=True)
            finally:
                os.close(descriptor)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def file_lock(folder):
    if fcntl is None:
        raise UpdateError("程序更新必须在支持 fcntl 的 Linux 上运行")
    with (Path(folder) / ".update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("已有其他更新进程运行") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

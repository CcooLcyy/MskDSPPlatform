#!/usr/bin/env python3
"""镜像内验证真实 Rust 程序下载、更新、gRPC 探测及持久化重启。"""
from contextlib import ExitStack
import gzip
import hashlib
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler

MANAGER_DIR = Path("/opt/mskdsp-manager")
sys.path.insert(0, str(MANAGER_DIR if MANAGER_DIR.exists() else Path(__file__).resolve().parent))
import runtime
import update_common as common

SEED = Path("/usr/local/bin/mskdsp-platform")
CONFIG = Path("/app/config.toml")
COOKIES = CookieJar()
OPENER = build_opener(ProxyHandler({}), HTTPCookieProcessor(COOKIES))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def request_json(url, method="GET", payload=None, action="update"):
    request = Request(url, data=json.dumps(payload or {}).encode() if method == "POST" else None, method=method,
                      headers={"X-MskDSP-Action": action, "Content-Type": "application/json"})
    with OPENER.open(request, timeout=3) as response:
        return json.loads(response.read(common.MAX_MANIFEST + 1))


def require_login(control):
    try:
        request_json(control + "/api/update/status")
    except HTTPError as exc:
        try:
            require(exc.code == 401, "未登录更新接口应返回 401")
        finally:
            exc.close()
    else:
        raise RuntimeError("未登录会话被错误允许访问更新接口")


def serve(server, stack):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    stack.callback(stop)
    return f"http://127.0.0.1:{server.server_port}"


def old_seed(destination, username, password):
    """旧程序仅用于测试起点；新版所有检查使用实际 Rust 可执行文件。"""
    identity = {"service": common.PROJECT, "protocol_version": "platform.v1", "base_version": "0.0.0",
                "version": "0.0.0-dev", "channel": "dev", "commit": "unknown", "build_time": "unknown"}
    script = f'''#!{sys.executable}
import json, os, sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
identity = {identity!r}
username, password = {username!r}, {password!r}
session = ""
if "--version-json" in sys.argv:
    print(json.dumps(identity)); raise SystemExit(0)
if "--probe-grpc" in sys.argv:
    raise SystemExit(0)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/v1/auth/me":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("mskdsp_admin_session")
            valid = bool(session) and token is not None and token.value == session
            self.reply(200 if valid else 401, {{"username": username, "role": "admin"}} if valid else {{"error": "请先登录"}})
            return
        value = identity if self.path == "/api/v1/version" else {{"status": "ready", "online_agents": 0}}
        if self.path not in ("/healthz", "/readyz", "/api/v1/version"):
            self.send_error(404); return
        self.reply(200, value)
    def do_POST(self):
        global session
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path != "/api/v1/auth/login":
            self.send_error(404); return
        if self.headers.get("X-MskDSP-Action") != "auth" or body != {{"username": username, "password": password}}:
            self.reply(401, {{"error": "登录失败"}}); return
        session = "old-smoke-session"
        self.reply(200, {{"username": username, "role": "admin"}}, "mskdsp_admin_session=" + session + "; Path=/; HttpOnly")
    def reply(self, status, value, cookie=None):
        raw = json.dumps(value).encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def log_message(self, *args):
        pass
host, port = os.environ["MSKDSP_PLATFORM_HTTP_ADDR"].rsplit(":", 1)
ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
'''
    destination.write_text(script, encoding="utf-8")
    destination.chmod(0o755)
    return identity


def binary_archive(destination, binary):
    with destination.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            item = tarfile.TarInfo(common.PROJECT)
            item.mode, item.size = 0o755, binary.stat().st_size
            with binary.open("rb") as stream:
                archive.addfile(item, stream)


def wait_job(manager, expected, deadline):
    while time.monotonic() < deadline:
        status = manager.status()
        if not manager.busy:
            require(status["state"] == expected, "更新任务状态异常：" + json.dumps(status, ensure_ascii=False))
            return
        time.sleep(0.05)
    raise RuntimeError("等待更新任务超时")


class SmokeManager(runtime.Manager):
    def healthy(self, state=None, timeout=30):
        # 保留全部 HTTP 身份和真实 gRPC 检查，仅缩短烟测失败时的等待。
        return super().healthy(state, timeout=min(timeout, 8))


def run_smoke():
    common.platform_key()
    common.verify_elf(SEED)
    identity = runtime.binary_identity(SEED)
    common.validate_commit(identity["channel"], identity["version"], identity["commit"])
    config_before = CONFIG.read_bytes()
    deadline = time.monotonic() + 35
    with tempfile.TemporaryDirectory(prefix="platform-runtime-smoke-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        config = root / "config.toml"
        shutil.copyfile(CONFIG, config)
        # 生成临时测试凭据及 Rust 真实哈希，现场配置和默认管理员密码不参与烟测。
        username, password = "runtime-smoke-admin", secrets.token_urlsafe(24)
        hashed = subprocess.run([str(SEED), "--hash-admin-password"], input=password, text=True,
                                encoding="utf-8", capture_output=True, check=True, timeout=10).stdout.strip()
        require(hashed.startswith("$argon2id$"), "测试管理员密码哈希格式不正确")
        original = re.sub(r"(?ms)^\[\s*auth\s*\][^\n]*\n.*?(?=^\[|\Z)", "", config.read_text(encoding="utf-8"))
        config.write_text(original.rstrip() + "\n\n[auth]\nusername = " + json.dumps(username)
                          + "\npassword_hash = " + json.dumps(hashed)
                          + "\nsecure_cookie = false\nsession_seconds = 600\n", encoding="utf-8")
        config_copy_before = config.read_bytes()
        seed = root / "old-platform"
        previous = old_seed(seed, username, password)
        archive = root / "binary.tar.gz"
        binary_archive(archive, SEED)
        with archive.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {"schema_version": 1, "project": common.PROJECT,
                    **{key: identity[key] for key in runtime.IDENTITY_FIELDS},
                    "base_version": identity.get("base_version", identity["version"].split("-")[0]),
                    "published_at": identity["build_time"], "platforms": {"linux-amd64": {}}}
        channel_path = f"/mskdsp-platform/{identity['channel']}"
        latest_path, binary_path = channel_path + "/latest.json", channel_path + "/candidate/binary.tar.gz"
        reads = []
        class Assets(BaseHTTPRequestHandler):
            def do_GET(self):
                reads.append(self.path)
                if self.path == latest_path:
                    raw = json.dumps(manifest).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif self.path == binary_path:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(archive.stat().st_size))
                    self.end_headers()
                    with archive.open("rb") as source:
                        shutil.copyfileobj(source, self.wfile)
                else:
                    self.send_error(404)
            def log_message(self, *args):
                pass
        asset_base = serve(ThreadingHTTPServer(("127.0.0.1", 0), Assets), stack)
        manifest["platforms"]["linux-amd64"] = {"binary_url": asset_base + binary_path,
                                                      "binary_sha256": checksum, "binary_size": archive.stat().st_size}
        updater = root / "updater.json"
        updater.write_text(json.dumps({"manifest_url": asset_base + latest_path}), encoding="utf-8")
        transport = common.Transport(allow_loopback_http=True)
        common.validate_manifest(manifest, asset_base + latest_path, transport=transport)
        manager = SmokeManager(root / "runtime", updater, config=config, seed=seed, transport=transport)
        stack.callback(manager.close)
        manager.start()
        old_process = manager.child
        require(manager.current["version"] == previous["version"], "旧程序起点身份错误")
        control = serve(runtime.RuntimeServer(("127.0.0.1", 0), manager), stack)
        COOKIES.clear()
        require_login(control)
        request_json(control + "/api/v1/auth/login", "POST", {"username": username, "password": password}, "auth")
        request_json(control + "/api/update/check", "POST")
        wait_job(manager, "available", deadline)
        require(manager.child is old_process and old_process.poll() is None, "检查更新不应停止旧程序")
        require(binary_path not in reads, "手动检查不应下载程序")
        request_json(control + "/api/update/apply", "POST")
        wait_job(manager, "succeeded", deadline)
        require(binary_path in reads, "程序更新未经过真实 HTTP 下载")
        require(old_process.poll() is not None, "更新后旧程序未回收")
        installed = json.loads((manager.runtime_dir / "state.json").read_text(encoding="utf-8"))
        require(installed["previous"]["release"] == "seed", "旧版本未保留为回滚起点")
        require(installed["binary_sha256"] == checksum, "状态中的程序摘要不一致")
        for key in runtime.IDENTITY_FIELDS:
            require(installed[key] == identity[key], "持久化构建身份不一致：" + key)
        require(manager.binary_path() != seed, "新版仍使用旧种子程序")
        require(request_json(control + "/api/v1/version")["version"] == identity["version"], "HTTP 代理未切换到新版")
        require_login(control)
        request_json(control + "/api/v1/auth/login", "POST", {"username": username, "password": password}, "auth")
        request_json(control + "/api/update/check", "POST")
        wait_job(manager, "current", deadline)
        new_process = manager.child
        manager.close()
        require(new_process.poll() is not None, "管理进程关闭后真实 Rust 子进程未回收")
        restarted = SmokeManager(manager.runtime_dir, updater, config=config, seed=seed, transport=transport)
        stack.callback(restarted.close)
        require(restarted.current["release"] != "seed", "重启用旧种子覆盖了持久化新版")
        restarted.start()
        require(restarted.healthy(timeout=3), "持久化重启未通过真实 HTTP/gRPC 检查")
        require(restarted.current["version"] == identity["version"], "重启后构建身份丢失")
        require(config.read_bytes() == config_copy_before and CONFIG.read_bytes() == config_before, "更新修改了现场配置")
        require(time.monotonic() < deadline, "真实更新烟测超过时间上限")
    print("单容器更新烟测通过：真实 Rust 下载、SHA256/ELF/CLI 校验、手动 HTTP 更新、真实 gRPC RPC、旧进程回收及持久化重启。")
    print("测试范围：旧版本起点为 Python 测试替身；候选与重启为镜像内真实 Rust 程序；失败回滚由独立单元测试验证。")


if __name__ == "__main__":
    run_smoke()

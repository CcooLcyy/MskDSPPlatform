#!/usr/bin/env python3
"""单容器平台管理进程：手动更新、HTTP 代理与子进程监督。"""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit
from urllib.request import urlopen
import uuid

import update_common as common

IDENTITY_FIELDS = ("version", "channel", "commit", "build_time")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
BUSY_STATES = {"checking", "updating"}
AUTH_TIMEOUT = 2
MAX_AUTH_RESPONSE = 4096


class BusyError(common.UpdateError):
    pass


def log(message):
    print("[平台管理] " + message, flush=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise common.UpdateError("读取 JSON 文件失败：" + str(path)) from exc


def release_name(version):
    name = version.replace("+", "_")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise common.UpdateError("发布版本路径无效")
    return name


def probe_address(config_path):
    try:
        config = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        value = config.get("server", {}).get("grpc_addr", "127.0.0.1:50000")
        host, port = listen_address(value)
        host = {"0.0.0.0": "127.0.0.1", "[::]": "[::1]"}.get(host, host)
        return f"{host}:{port}"
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise common.UpdateError("读取 gRPC 健康检查地址失败：" + str(exc)) from exc


def binary_identity(path):
    try:
        response = subprocess.run([str(path), "--version-json"], check=True, capture_output=True,
                                  text=True, encoding="utf-8", timeout=10)
        if len(response.stdout) > common.MAX_MANIFEST:
            raise common.UpdateError("程序身份输出过大")
        identity = json.loads(response.stdout)
        if not isinstance(identity, dict) or identity.get("service") != common.PROJECT or any(not isinstance(identity.get(key), str) for key in IDENTITY_FIELDS):
            raise common.UpdateError("程序身份输出格式不正确")
        return identity
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise common.UpdateError("程序版本检查失败：" + str(exc)) from exc


def verify_candidate(path, manifest):
    identity = binary_identity(path)
    for key in IDENTITY_FIELDS:
        expected = manifest.get(key, manifest.get("published_at"))
        if identity[key] != expected:
            raise common.UpdateError("候选程序构建身份与清单不一致：" + key)
    common.validate_commit(identity["channel"], identity["version"], identity["commit"])
    return identity


class Manager:
    def __init__(self, runtime_dir, updater_config, config="/app/config.toml",
                 seed="/usr/local/bin/mskdsp-platform", child_http="127.0.0.1:8081",
                 grpc_probe=None, transport=None):
        self.runtime_dir, self.seed = Path(runtime_dir), Path(seed)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "releases").mkdir(exist_ok=True)
        if self.runtime_dir.is_symlink() or (self.runtime_dir / "releases").is_symlink():
            raise common.UpdateError("运行状态目录不能是符号链接")
        self.config, self.child_http = str(config), child_http
        self.grpc_probe = grpc_probe if grpc_probe is not None else probe_address(config)
        child_host, _ = listen_address(child_http)
        if child_host != "127.0.0.1":
            raise common.UpdateError("平台内部 HTTP 必须监听 127.0.0.1")
        self.transport = transport or common.Transport()
        self.guard, self.lifecycle = threading.RLock(), threading.RLock()
        self.stopping = threading.Event()
        self.child = self.worker = self.monitor = None
        self.busy, self.source_url = False, ""
        config_error = ""
        if Path(updater_config).exists():
            try:
                settings = read_json(updater_config)
                source = settings.get("manifest_url", "") if isinstance(settings, dict) else ""
                parsed = self.transport.validate_url(source)
                if parsed.path.split("/")[-2:] not in (["ci", "latest.json"], ["beta", "latest.json"], ["stable", "latest.json"]):
                    raise common.UpdateError("更新源必须是通道 latest.json")
                self.source_url = source
            except common.UpdateError as exc:
                config_error = "更新配置无效：" + str(exc)
        state = self.runtime_dir / "state.json"
        if state.exists():
            self.current = read_json(state)
            self.validate_installed(self.current)
        else:
            self.current = {**binary_identity(self.seed), "release": "seed"}
        self.result = dict(latest_version="", state="idle", message=config_error or "等待手动检查更新",
                           update_available=False, job_id="")
        task = self.runtime_dir / "task.json"
        if task.exists():
            saved = read_json(task)
            if not isinstance(saved, dict):
                raise common.UpdateError("更新任务状态格式不正确")
            self.result.update({key: saved[key] for key in self.result if key in saved})
            if self.result["state"] in BUSY_STATES:
                self.set_result(state="error", message="上次任务中断，已恢复最后提交版本", update_available=False)

    def validate_installed(self, state):
        if not isinstance(state, dict) or any(not isinstance(state.get(key), str) for key in (*IDENTITY_FIELDS, "release")):
            raise common.UpdateError("持久化安装状态不正确")
        if state["release"] == "seed":
            identity = binary_identity(self.seed)
            if any(identity[key] != state[key] for key in IDENTITY_FIELDS):
                raise common.UpdateError("基础镜像种子与持久化状态不一致，请恢复基础镜像")
            return
        if state["release"] != release_name(state["version"]):
            raise common.UpdateError("持久化版本路径不正确")
        common.validate_commit(state["channel"], state["version"], state["commit"])
        path = self.binary_path(state)
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise common.UpdateError("已安装二进制缺失或为链接")
        common.verify_elf(path)
        verify_candidate(path, state)

    def binary_path(self, state=None):
        state = state or self.current
        return self.seed if state["release"] == "seed" else self.runtime_dir / "releases" / state["release"] / common.PROJECT

    def status(self):
        with self.guard:
            channel = self.current["channel"]
            if channel == "dev" and self.source_url:
                channel = urlsplit(self.source_url).path.split("/")[-2]
            return dict(enabled=bool(self.source_url), current_version=self.current["version"],
                        source_url=self.source_url, channel=channel, **self.result)

    def set_result(self, **fields):
        with self.guard:
            self.result.update(fields)
            common.atomic_json(self.runtime_dir / "task.json", self.result)

    def ensure_running(self):
        if self.stopping.is_set():
            raise common.UpdateError("管理进程正在退出，更新任务已取消")

    def begin(self, operation):
        with self.guard:
            if self.busy or self.stopping.is_set():
                raise BusyError("已有任务运行或管理进程正在退出")
            if operation not in ("check", "apply") or not self.source_url:
                raise common.UpdateError("更新操作无效或没有有效更新源")
            self.busy = True
            try:
                self.set_result(state="checking" if operation == "check" else "updating", message="正在检查更新清单", job_id=uuid.uuid4().hex)
                self.worker = threading.Thread(target=self.run_job, args=(operation,), daemon=True)
                self.worker.start()
            except Exception:
                self.busy = False
                raise

    def prepare(self, manifest, artifact):
        release = release_name(manifest["version"])
        destination = self.runtime_dir / "releases" / release
        with tempfile.TemporaryDirectory(prefix=".download-", dir=self.runtime_dir) as temporary:
            archive = common.download_artifact(artifact, temporary, self.source_url, self.transport)
            self.ensure_running()
            candidate = Path(temporary) / "candidate"
            binary = common.extract_binary(archive, candidate)
            common.verify_elf(binary)
            verify_candidate(binary, manifest)
            self.ensure_running()
            protected = {self.current["release"], self.current.get("previous", {}).get("release")}
            if destination.exists():
                if destination.is_symlink() or release in protected:
                    raise common.UpdateError("候选路径与受保护版本冲突")
                shutil.rmtree(destination)
            os.replace(candidate, destination)
            if os.name == "posix":
                descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return destination

    def run_job(self, operation):
        committed = False
        try:
            with common.file_lock(self.runtime_dir):
                self.ensure_running()
                manifest = self.transport.fetch_manifest(self.source_url)
                key = common.platform_key()
                artifact = common.validate_manifest(manifest, self.source_url, key, self.transport)
                available = common.needs_update(manifest, self.current)
                self.set_result(latest_version=manifest["version"], update_available=available)
                if not available:
                    self.set_result(state="current", message="当前已是最新版本")
                    return
                if operation == "check":
                    self.set_result(state="available", message="发现新版本，可手动更新")
                    return
                self.set_result(state="updating", message="正在下载校验，当前平台继续服务")
                candidate = self.prepare(manifest, artifact)
                self.ensure_running()
                old = copy.deepcopy(self.current)
                next_state = {field: manifest.get(field, manifest.get("published_at")) for field in IDENTITY_FIELDS}
                next_state.update(release=candidate.name, platform=key, binary_sha256=artifact["binary_sha256"],
                                  previous={field: value for field, value in old.items() if field != "previous"})
                with self.lifecycle:
                    self.ensure_running()
                    self.set_result(message="校验通过，正在启动新平台进程")
                    self.stop_child()
                    try:
                        self.launch(next_state)
                        if not self.healthy(next_state):
                            raise common.UpdateError("新版本 HTTP/gRPC 健康检查失败")
                        self.ensure_running()
                        common.atomic_json(self.runtime_dir / "state.json", next_state)
                        committed = True
                    except Exception as exc:
                        self.stop_child()
                        if self.stopping.is_set():
                            raise common.UpdateError("任务取消，下次启动恢复最后提交版本") from exc
                        try:
                            self.launch(old)
                            if not self.healthy(old):
                                raise common.UpdateError("旧版本 HTTP/gRPC 健康检查失败")
                        except Exception as rollback:
                            raise common.UpdateError("更新失败且回滚失败，需人工处理：" + str(rollback)) from exc
                        raise common.UpdateError("更新失败，已回滚：" + str(exc)) from exc
                    with self.guard:
                        self.current = next_state
                    self.set_result(state="succeeded", message="更新成功：" + next_state["version"], update_available=False)
                self.prune()
        except Exception as exc:
            message = ("新版已提交，但后续任务处理失败：" if committed else "") + str(exc)
            log(message)
            try:
                self.set_result(state="error", message=message)
            except OSError as error:
                log("保存任务结果失败：" + str(error))
        finally:
            with self.guard:
                self.busy = False

    def prune(self):
        keep = {self.current["release"], self.current.get("previous", {}).get("release")}
        for path in (self.runtime_dir / "releases").iterdir():
            if path.name not in keep and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)

    def launch(self, state=None):
        self.ensure_running()
        env = dict(os.environ, MSKDSP_PLATFORM_CONFIG=self.config, MSKDSP_PLATFORM_HTTP_ADDR=self.child_http)
        binary = self.binary_path(state)
        log("启动平台子进程：" + str(binary))
        self.child = subprocess.Popen([str(binary)], env=env, stdin=subprocess.DEVNULL)

    def stop_child(self):
        child, self.child = self.child, None
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            else:
                child.wait()

    def healthy(self, state=None, timeout=30):
        state = state or self.current
        deadline = time.monotonic() + timeout
        while not self.stopping.is_set() and time.monotonic() < deadline:
            if self.child is None or self.child.poll() is not None:
                return False
            try:
                with urlopen("http://" + self.child_http + "/readyz", timeout=2) as response:
                    ready = json.loads(response.read(common.MAX_MANIFEST + 1))
                if not isinstance(ready, dict) or ready.get("status") != "ready":
                    raise common.UpdateError("平台未就绪")
                with urlopen("http://" + self.child_http + "/api/v1/version", timeout=2) as response:
                    identity = json.loads(response.read(common.MAX_MANIFEST + 1))
                if not isinstance(identity, dict) or identity.get("service") != common.PROJECT or any(identity.get(key) != state[key] for key in IDENTITY_FIELDS):
                    raise common.UpdateError("运行中平台身份不一致")
                probe = subprocess.run([str(self.binary_path(state)), "--probe-grpc", self.grpc_probe],
                                       env=dict(os.environ, MSKDSP_PLATFORM_CONFIG=self.config),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if probe.returncode == 0 and self.child.poll() is None:
                    return True
            except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
                pass
            self.stopping.wait(0.3)
        return False

    def start(self):
        with self.lifecycle:
            with common.file_lock(self.runtime_dir):
                for path in self.runtime_dir.glob(".download-*"):
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                self.prune()
            self.launch()
            if not self.healthy():
                self.stop_child()
                raise common.UpdateError("平台启动失败，HTTP/gRPC 未就绪")
            if not (self.runtime_dir / "state.json").exists():
                common.atomic_json(self.runtime_dir / "state.json", self.current)
        self.monitor = threading.Thread(target=self.supervise, daemon=True)
        self.monitor.start()

    def supervise(self):
        failures = []
        while not self.stopping.wait(1):
            with self.lifecycle:
                if self.stopping.is_set() or self.child is None or self.child.poll() is None:
                    continue
                code = self.child.returncode
                self.stop_child()
                now = time.monotonic()
                failures = [stamp for stamp in failures if now - stamp < 60]
                if len(failures) >= 3:
                    self.set_result(state="error", message="平台一分钟内多次退出，已停止自动重启")
                    log("子进程异常退出，停止自动重启")
                    continue
                failures.append(now)
                log("平台异常退出（" + str(code) + "），恢复最后提交版本")
                try:
                    self.launch()
                    if not self.healthy(timeout=10):
                        raise common.UpdateError("恢复健康检查失败")
                except Exception as exc:
                    self.stop_child()
                    self.set_result(state="error", message="平台恢复失败：" + str(exc))

    def close(self):
        self.stopping.set()
        with self.lifecycle:
            self.stop_child()
        if self.worker:
            self.worker.join(timeout=3)
        if self.monitor:
            self.monitor.join(timeout=2)


class RuntimeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, manager):
        self.manager = manager
        super().__init__(address, RuntimeHandler)


class RuntimeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        log("HTTP " + fmt % args)

    def respond(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(raw)

    def body(self, limit):
        lengths = self.headers.get_all("Content-Length", [])
        length = lengths[0] if lengths else "0"
        if self.headers.get("Transfer-Encoding") or len(lengths) > 1 or not re.fullmatch(r"[0-9]+", length):
            raise ValueError("请求长度或传输编码不支持")
        length = int(length)
        if length > limit:
            raise OverflowError("请求超过大小限制")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求内容不完整")
        return raw

    def action_allowed(self):
        if self.headers.get_all("X-MskDSP-Action", []) != ["update"]:
            return False
        origins, hosts = self.headers.get_all("Origin", []), self.headers.get_all("Host", [])
        if len(hosts) != 1 or len(origins) > 1:
            return False
        if not origins:
            return True
        try:
            origin, host = urlsplit(origins[0]), urlsplit("//" + hosts[0])
            return (origin.scheme in ("http", "https") and not origin.username and not origin.password
                    and not origin.path and not origin.query and not origin.fragment
                    and origin.hostname == host.hostname
                    and (origin.port or (443 if origin.scheme == "https" else 80)) == (host.port or (443 if origin.scheme == "https" else 80)))
        except ValueError:
            return False

    def authenticated(self):
        # 会话只由 Rust 验证；连接失败、未知身份和异常响应一律关闭更新入口。
        connection = http.client.HTTPConnection(self.server.manager.child_http, timeout=AUTH_TIMEOUT)
        try:
            headers = {"Connection": "close"}
            cookies = self.headers.get_all("Cookie", [])
            if cookies:
                headers["Cookie"] = "; ".join(cookies)
            connection.request("GET", "/api/v1/auth/me", headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_AUTH_RESPONSE + 1)
            if len(raw) > MAX_AUTH_RESPONSE:
                raise ValueError("会话验证响应超过大小限制")
            if response.length:
                raise ValueError("会话验证响应内容不完整")
            identity = json.loads(raw)
            if (response.status == 200 and isinstance(identity, dict)
                    and isinstance(identity.get("username"), str) and identity["username"].strip()
                    and identity.get("role") == "admin"):
                return True
            if response.status == 401 and isinstance(identity, dict) and isinstance(identity.get("error"), str):
                self.respond(401, {"error": "请先登录"})
                return False
            raise ValueError("会话验证响应不正确")
        except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
            log("更新会话验证失败：" + str(exc))
            self.respond(503, {"error": "平台会话验证暂时不可用"})
            return False
        finally:
            connection.close()

    def handle_request(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/update/"):
            if not self.authenticated():
                return
            if parsed.query or parsed.fragment:
                self.respond(400, {"error": "更新接口不接受查询参数"})
            elif parsed.path == "/api/update/status":
                self.respond(200, self.server.manager.status()) if self.command == "GET" else self.respond(405, {"error": "状态接口仅接受 GET"})
            elif parsed.path not in ("/api/update/check", "/api/update/apply"):
                self.respond(404, {"error": "更新接口不存在"})
            elif self.command != "POST":
                self.respond(405, {"error": "更新操作仅接受 POST"})
            elif not self.action_allowed():
                self.respond(403, {"error": "缺少操作标记或跨源请求"})
            else:
                try:
                    raw = self.body(1024)
                    if raw and json.loads(raw) != {}:
                        raise ValueError("更新操作不接受参数")
                    # 上传请求体期间会话可能被注销，提交更新前必须再次校验。
                    if not self.authenticated():
                        return
                    self.server.manager.begin(parsed.path.rsplit("/", 1)[1])
                    self.respond(202, self.server.manager.status())
                except BusyError as exc:
                    self.respond(409, {"error": str(exc)})
                except OverflowError as exc:
                    self.respond(413, {"error": str(exc)})
                except common.UpdateError as exc:
                    self.respond(503, {"error": str(exc)})
                except (ValueError, OSError) as exc:
                    self.respond(400, {"error": str(exc)})
            return
        self.proxy()

    def proxy(self):
        connection = http.client.HTTPConnection(self.server.manager.child_http, timeout=10)
        sent = False
        try:
            # 点表与上位机一致支持 2 MiB，其他代理请求沿用原有上限。
            path = urlsplit(self.path).path
            template_request = path == "/api/v1/modbus-templates" or path.startswith("/api/v1/modbus-templates/")
            raw = self.body((2 if template_request else 1) * 1024 * 1024)
            blocked = HOP_HEADERS | {part.strip().lower() for part in self.headers.get("Connection", "").split(",")}
            # 保留浏览器访问的 Host 以便 Rust 校验 Origin，丢弃客户端伪造的代理身份头。
            headers = {name: value for name, value in self.headers.items()
                       if name.lower() not in blocked and name.lower() != "forwarded"
                       and not name.lower().startswith("x-forwarded-")}
            headers["Connection"] = "close"
            connection.request(self.command, self.path, raw or None, headers)
            response = connection.getresponse()
            blocked = HOP_HEADERS | {part.strip().lower() for part in response.getheader("Connection", "").split(",")}
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in blocked and name.lower() not in ("server", "date"):
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sent = True
            if self.command != "HEAD":
                while not self.server.manager.stopping.is_set():
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OverflowError as exc:
            self.respond(413, {"error": str(exc)})
        except (OSError, ValueError, http.client.HTTPException) as exc:
            if not sent:
                self.respond(502, {"error": "平台暂时不可用：" + str(exc)})
        finally:
            connection.close()

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = handle_request


def listen_address(value):
    host, separator, port = value.rpartition(":")
    if not separator or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise common.UpdateError("监听地址必须为 host:port")
    return host, int(port)


def main():
    manager = server = None
    stopped = threading.Event()
    def stop(signum, frame):
        stopped.set()
        if manager:
            manager.stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        common.platform_key()
        config_path = os.environ.get("MSKDSP_PLATFORM_CONFIG", "/app/config.toml")
        manager = Manager(os.environ.get("MSKDSP_RUNTIME_DIR", "/var/lib/mskdsp-platform"),
                          os.environ.get("MSKDSP_UPDATER_CONFIG", "/app/updater.json"),
                          config=config_path,
                          child_http=os.environ.get("MSKDSP_CHILD_HTTP_ADDR", "127.0.0.1:8081"))
        manager.start()
        server = RuntimeServer(listen_address(os.environ.get("MSKDSP_MANAGER_LISTEN", "0.0.0.0:8080")), manager)
        server.timeout = 0.5
        log("网页管理服务已启动，等待手动操作")
        while not stopped.is_set():
            server.handle_request()
        return 0
    except (common.UpdateError, OSError, subprocess.SubprocessError) as exc:
        log("管理进程启动失败：" + str(exc))
        return 1
    finally:
        if manager:
            manager.close()
        if server:
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())

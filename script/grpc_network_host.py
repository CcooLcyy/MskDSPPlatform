#!/usr/bin/env python3
"""宿主机 gRPC 单向 MSS 管理服务，仅管理专属 nft 表。"""
import argparse
import copy
import http.server
import json
import logging
import os
import pathlib
import re
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import tempfile
import threading
import time


TABLE = "mskdsp_grpc_mss"
CONFIG_KEYS = {"enabled", "mss", "revision", "port", "external_interface", "bridge_interface"}
SETTING_KEYS = {"enabled", "mss", "revision"}
MAX_REVISION = 2**64 - 1
LOG = logging.getLogger("grpc-network")


class Invalid(ValueError):
    pass


class Conflict(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid("JSON 不允许重复字段")
            result[key] = value
        return result
    def constant(_value):
        raise Invalid("JSON 数值格式不合法")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise Invalid("JSON 格式不合法") from exc


def validate_settings(value):
    if type(value) is not dict or set(value) != SETTING_KEYS:
        raise Invalid("设置只允许 enabled、mss 和 revision 三个字段")
    if type(value["enabled"]) is not bool:
        raise Invalid("启用状态必须为布尔值")
    if type(value["mss"]) is not int or not 536 <= value["mss"] <= 1460:
        raise Invalid("MSS 必须为 536 至 1460 的整数")
    if type(value["revision"]) is not int or not 0 <= value["revision"] <= MAX_REVISION:
        raise Invalid("配置修订必须为有效非负整数")


def validate_config(value):
    if type(value) is not dict or set(value) != CONFIG_KEYS:
        raise Invalid("宿主机配置字段不完整或包含未知字段")
    validate_settings({key: value[key] for key in SETTING_KEYS})
    if type(value["port"]) is not int or value["port"] != 50000:
        raise Invalid("仅允许管理平台 gRPC 50000 端口")
    for key in ("external_interface", "bridge_interface"):
        if type(value[key]) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", value[key]):
            raise Invalid("宿主机网络接口名称不合法")
    if value["external_interface"] == value["bridge_interface"]:
        raise Invalid("外部接口与容器网桥不能相同")


def atomic_save(path, value):
    """同目录原子替换，配置正文和目录都完成同步才报告成功。"""
    validate_config(value)
    path = pathlib.Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".grpc-network-", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def render(config, exists):
    validate_config(config)
    prefix = f"delete table ip {TABLE}\n" if exists else ""
    if not config["enabled"]:
        return prefix
    external = config["external_interface"]
    bridge = config["bridge_interface"]
    mss = config["mss"]
    return prefix + f'''table ip {TABLE} {{
 chain forward {{
  type filter hook forward priority mangle; policy accept;
  iifname "{external}" oifname "{bridge}" ct direction original ct status dnat ct original proto-dst 50000 tcp dport 50000 tcp flags & (syn | rst | ack) == syn tcp option maxseg size > {mss} counter tcp option maxseg size set {mss} comment "平台 gRPC 单向 MSS 限制"
 }}
}}
'''


def expected_expressions(config):
    def match(left, right, op="=="):
        return {"match": {"op": op, "left": left, "right": right}}
    mss = config["mss"]
    return [
        match({"meta": {"key": "iifname"}}, config["external_interface"]),
        match({"meta": {"key": "oifname"}}, config["bridge_interface"]),
        match({"ct": {"key": "direction"}}, "original"),
        match({"ct": {"key": "status"}}, "dnat", "in"),
        match({"ct": {"key": "proto-dst", "dir": "original"}}, 50000),
        match({"payload": {"protocol": "tcp", "field": "dport"}}, 50000),
        match({"&": [{"payload": {"protocol": "tcp", "field": "flags"}},
                       {"|": ["syn", "rst", "ack"]}]}, "syn"),
        match({"tcp option": {"name": "maxseg", "field": "size"}}, mss, ">"),
        {"counter": {}},
        {"mangle": {"key": {"tcp option": {"name": "maxseg", "field": "size"}}, "value": mss}},
    ]


class Nft:
    def command(self, args, text=None):
        try:
            result = subprocess.run(["/usr/sbin/nft", *args], input=text,
                                    text=True, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Unavailable("宿主机 nft 命令无法执行或超时") from exc
        if result.returncode:
            raise Unavailable("宿主机 nft 操作失败")
        return result.stdout

    def table(self):
        result = parse_json(self.command(["-j", "list", "tables"]))
        exists = any(row.get("table", {}).get("family") == "ip" and
                     row.get("table", {}).get("name") == TABLE
                     for row in result["nftables"])
        if not exists:
            return None
        return parse_json(self.command(["-j", "list", "table", "ip", TABLE]))["nftables"]

    def apply(self, config):
        transaction = render(config, self.table() is not None)
        if transaction:
            # 一个 nft 批次原子提交，绝不清空或恢复整个防火墙。
            self.command(["-f", "-"], transaction)
        if not self.inspect(config)[0]:
            raise Unavailable("宿主机实际规则与保存设置不一致")

    def inspect(self, config):
        table = self.table()
        if not config["enabled"]:
            return table is None, 0
        if table is None:
            return False, 0
        chains = [row["chain"] for row in table if "chain" in row]
        rules = [row["rule"] for row in table if "rule" in row]
        if len(chains) != 1 or len(rules) != 1:
            return False, 0
        chain = chains[0]
        if any(chain.get(key) != expected for key, expected in
               dict(name="forward", type="filter", hook="forward", prio=-150, policy="accept").items()):
            return False, 0
        rule = rules[0]
        if rule.get("chain") != "forward":
            return False, 0
        expressions = copy.deepcopy(rule.get("expr", []))
        packets = 0
        for item in expressions:
            if "counter" in item:
                packets += item["counter"].get("packets", 0)
                item["counter"] = {}
        return expressions == expected_expressions(config), packets

    def cleanup(self):
        if self.table() is not None:
            self.command(["delete", "table", "ip", TABLE])


class Manager:
    def __init__(self, path, nft=None):
        self.path = pathlib.Path(path)
        self.nft = nft or Nft()
        self.config = None
        self.error = None
        self.persistent = False
        self.stopping = False
        self.lock = threading.RLock()

    def start(self):
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise Invalid("宿主机配置不是普通文件")
            if os.name == "posix":
                info = self.path.stat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise Invalid("宿主机配置必须由 root 管理且不可被其他用户写入")
            value = parse_json(self.path.read_text(encoding="utf-8"))
            validate_config(value)
            self.config = value
            self.persistent = True
            self.nft.apply(value)
            LOG.info("平台端口分段设置已恢复，启用=%s，MSS=%s，修订=%s",
                     value["enabled"], value["mss"], value["revision"])
        except Exception:
            self.error = "宿主机配置读取或规则应用失败，请检查独立服务日志和 root 配置"
            LOG.exception("平台端口分段设置恢复失败")

    def status(self):
        with self.lock:
            config = self.config or dict(enabled=False, mss=1240, revision=0, port=50000)
            result = {key: config[key] for key in ("enabled", "mss", "revision", "port")}
            result.update(persistent=self.persistent, applied=False, matched_packets=0)
            error = self.error
            if self.config is not None:
                try:
                    applied, packets = self.nft.inspect(self.config)
                    result.update(applied=applied, matched_packets=packets)
                    if not applied:
                        error = error or "宿主机实际规则尚未应用或与配置不一致"
                except Exception:
                    error = "无法核验宿主机实际规则"
            if error:
                result["error"] = error
            return result

    def update(self, request):
        validate_settings(request)
        with self.lock:
            if self.stopping:
                raise Unavailable("宿主机网络管理服务正在停止")
            if self.config is None:
                raise Unavailable("宿主机配置损坏或缺失，须由管理员修复")
            if request["revision"] != self.config["revision"]:
                raise Conflict("配置已变化，请重新读取后保存")
            if self.config["revision"] == MAX_REVISION:
                raise Conflict("配置修订已达到上限")
            previous = dict(self.config)
            next_config = dict(previous, **request)
            next_config["revision"] += 1
            started = time.monotonic()
            try:
                self.nft.apply(next_config)
            except Exception as exc:
                self.error = "规则应用失败，配置未保存"
                # nft 批次失败通常保持旧规则；读取核验失败时也尝试恢复旧规则。
                try:
                    self.nft.apply(previous)
                except Exception:
                    self.error = "规则应用失败且旧规则恢复未通过，请检查宿主机服务"
                LOG.exception("平台端口分段规则应用失败")
                raise Unavailable(self.error) from exc
            finally:
                LOG.info("平台端口分段处理耗时，阶段=应用规则，耗时毫秒=%.3f",
                         (time.monotonic() - started) * 1000)
            started = time.monotonic()
            try:
                atomic_save(self.path, next_config)
            except Exception as exc:
                errors = []
                try:
                    self.nft.apply(previous)
                except Exception:
                    errors.append("旧规则恢复失败")
                try:
                    atomic_save(self.path, previous)
                except Exception:
                    errors.append("旧配置恢复失败")
                    self.persistent = False
                self.error = "设置持久化失败" + ("；" + "；".join(errors) if errors else "，已恢复原设置")
                LOG.exception("平台端口分段设置持久化失败")
                raise Unavailable(self.error) from exc
            finally:
                LOG.info("平台端口分段处理耗时，阶段=持久化，耗时毫秒=%.3f",
                         (time.monotonic() - started) * 1000)
            self.config = next_config
            self.persistent = True
            self.error = None
            LOG.info("平台端口分段设置已保存，启用=%s，MSS=%s，修订=%s",
                     next_config["enabled"], next_config["mss"], next_config["revision"])
            started = time.monotonic()
            try:
                return self.status()
            finally:
                LOG.info("平台端口分段处理耗时，阶段=核验状态，耗时毫秒=%.3f",
                         (time.monotonic() - started) * 1000)

    def close(self):
        with self.lock:
            self.stopping = True
            self.nft.cleanup()


def peer_allowed(uid, platform_uid):
    return uid in (0, platform_uid)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MskDSPNetwork/1"

    def log_message(self, _format, *_args):
        # 不记录 HTTP 请求体和任意客户端提供的路径。
        pass

    def respond(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 响应发送失败不改变已经提交的设置，也不向断开的连接二次写入错误。
            self.close_connection = True
            LOG.warning("客户端已关闭连接，响应未送达；已完成的设置操作保持原结果")

    def send_error(self, code, message=None, explain=None):
        self.respond(code, {"error": "请求格式不合法或方法不支持"})

    def authorized(self):
        try:
            raw = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            good = peer_allowed(uid, self.server.platform_uid)
        except (OSError, AttributeError):
            good = False
        if not good:
            self.respond(403, {"error": "仅允许平台管理进程访问宿主机网络设置"})
        return good

    def do_GET(self):
        if not self.authorized():
            return
        if self.path != "/status":
            self.respond(404, {"error": "管理接口不存在"})
            return
        self.respond(200, self.server.manager.status())

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/settings":
            self.respond(404, {"error": "管理接口不存在"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                raise Invalid("请求必须提供唯一的正文长度")
            length = int(lengths[0])
            if not 1 <= length <= 1024:
                raise Invalid("设置请求正文长度不合法")
            if self.headers.get_content_type() != "application/json":
                raise Invalid("设置请求必须为 JSON")
            body = self.rfile.read(length)
            if len(body) != length:
                raise Invalid("设置请求正文不完整")
            self.respond(200, self.server.manager.update(parse_json(body)))
        except Invalid as exc:
            self.respond(400, {"error": str(exc)})
        except Conflict as exc:
            self.respond(409, {"error": str(exc)})
        except Unavailable as exc:
            self.respond(503, {"error": str(exc)})
        except (OSError, ValueError):
            self.respond(503, {"error": "宿主机网络设置暂时不可用"})


class UnixServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = getattr(socket, "AF_UNIX", None)
    daemon_threads = True
    block_on_close = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


def main():
    parser = argparse.ArgumentParser(description="宿主机平台 gRPC 单向 MSS 管理")
    parser.add_argument("--config", default="/etc/mskdsp-platform/grpc-network.json")
    parser.add_argument("--socket", default="/home/daniel/mskplt/runtime/network-control/control.sock")
    parser.add_argument("--uid", type=int, default=10001)
    parser.add_argument("--cleanup", action="store_true", help="仅删除本服务专属规则表")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.name != "posix" or os.geteuid() != 0 or not 1 <= args.uid <= 2**31 - 1:
        parser.error("宿主机服务必须以 root 运行，平台 UID 必须为有效正整数")
    if args.cleanup:
        Nft().cleanup()
        return
    import fcntl
    os.umask(0o077)
    address = pathlib.Path(args.socket)
    address.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = address.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("套接字目录必须是 root 管理的真实目录")
    os.chmod(address.parent, 0o755)
    with (address.parent / ".service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if address.exists() or address.is_symlink():
            info = address.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise RuntimeError("拒绝覆盖非本服务套接字文件")
            address.unlink()
        manager = Manager(args.config)
        server = None
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        try:
            manager.start()
            server = UnixServer(str(address), Handler)
            server.manager = manager
            server.platform_uid = args.uid
            server.timeout = 0.5
            os.chown(address, 0, args.uid)
            os.chmod(address, 0o660)
            LOG.info("平台端口分段管理本地接口已就绪")
            while not stopping.is_set():
                server.handle_request()
        finally:
            if server is not None:
                server.server_close()
            # 等待正在保存的请求退出，避免停止清理后又产生规则。
            manager.close()
            try:
                address.unlink()
            except FileNotFoundError:
                pass
            LOG.info("平台端口分段管理已停止，专属规则已清理")


if __name__ == "__main__":
    main()

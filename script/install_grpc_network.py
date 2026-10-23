#!/usr/bin/env python3
"""在 Linux 宿主机安装平台专用 gRPC 网络管理服务。"""
import argparse
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile
import socket
import time

SERVICE = "mskdsp-platform-grpc-network.service"
MARKER = "# MSKDSP_PLATFORM_GRPC_NETWORK"
LIBRARY = Path("/usr/local/lib/mskdsp-platform/grpc_network_host.py")
CONFIG = Path("/etc/mskdsp-platform/grpc-network.json")
UNIT = Path("/etc/systemd/system") / SERVICE


def configuration(args):
    if args.port != 50000:
        raise ValueError("仅允许管理平台 gRPC 50000 端口")
    if not 536 <= args.mss <= 1460:
        raise ValueError("MSS 必须为 536 至 1460")
    if not 1 <= args.uid <= 2**31 - 1:
        raise ValueError("平台 UID 必须为非 root 用户")
    for value in (args.external_interface, args.bridge_interface):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", value):
            raise ValueError("网络接口名不合法")
    work = PurePosixPath(args.work_dir)
    if not work.is_absolute() or work == PurePosixPath("/") or ".." in work.parts:
        raise ValueError("平台工作目录必须为非根目录的绝对路径")
    if any(char in str(work) for char in ("\n", "\r", "\x00")):
        raise ValueError("平台工作目录含不支持的字符")
    value = dict(enabled=args.enable, mss=args.mss, revision=0, port=args.port,
                 external_interface=args.external_interface, bridge_interface=args.bridge_interface)
    from grpc_network_host import validate_config
    validate_config(value)
    return value


def unit_source(work_dir, uid):
    directory = PurePosixPath(work_dir) / "runtime" / "network-control"
    arguments = ["/usr/bin/python3", LIBRARY.as_posix(), "--config", CONFIG.as_posix(),
                 "--socket", str(directory / "control.sock"), "--uid", str(uid)]
    # systemd 自己解析参数；百分号必须转义以避免展开单元标识。
    command = " ".join(shlex.quote(x.replace("%", "%%")) for x in arguments)
    writable = " ".join(shlex.quote(x.as_posix().replace("%", "%%")) for x in (CONFIG.parent, directory))
    return f"""{MARKER}
[Unit]
Description=MskDSP 平台 gRPC 端口分段管理
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart={command}
ExecStopPost={command} --cleanup
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=CAP_NET_ADMIN CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={writable}
PrivateTmp=true
RestrictAddressFamilies=AF_UNIX AF_NETLINK
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictNamespaces=true

[Install]
WantedBy=multi-user.target
"""


def atomic_write(path, data, mode=0o600):
    if path.is_symlink():
        raise ValueError("安装目标不能是符号链接：" + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("安装目录不能是符号链接：" + str(path.parent))
    descriptor, temporary = tempfile.mkstemp(prefix=".network-install-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(args):
    defaults = configuration(args)
    if os.geteuid() != 0:
        raise ValueError("安装宿主机网络管理服务需要 root 权限")
    source = Path(__file__).resolve().with_name("grpc_network_host.py")
    if not source.is_file():
        raise ValueError("安装包缺少宿主机网络管理程序")
    if UNIT.exists() and MARKER not in UNIT.read_text(encoding="utf-8"):
        raise ValueError("同名单元不属于平台网络管理，拒绝覆盖")
    if CONFIG.is_symlink():
        raise ValueError("已有网络设置不能是符号链接")
    # 已有设置必须保留；损坏时交给管理服务明确报错，不能静默覆盖。
    if CONFIG.exists():
        import grpc_network_host
        grpc_network_host.validate_config(json.loads(CONFIG.read_text(encoding="utf-8")))
    else:
        atomic_write(CONFIG, (json.dumps(defaults, ensure_ascii=False, indent=2) + "\n").encode())
    directory = Path(args.work_dir).resolve() / "runtime" / "network-control"
    if directory.is_symlink():
        raise ValueError("控制套接字目录不能是符号链接")
    directory.mkdir(parents=True, exist_ok=True)
    os.chown(directory, 0, 0)
    os.chmod(directory, 0o755)
    atomic_write(LIBRARY, source.read_bytes(), 0o644)
    atomic_write(UNIT, unit_source(Path(args.work_dir).resolve().as_posix(), args.uid).encode(), 0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=20)
    subprocess.run(["systemctl", "enable", SERVICE], check=True, timeout=20)
    subprocess.run(["systemctl", "restart", SERVICE], check=True, timeout=25)
    wait_ready(directory / "control.sock")
    print(json.dumps(dict(service=SERVICE, config=str(CONFIG), socket=str(directory / "control.sock"),
                          persistent=True), ensure_ascii=False))


def wait_ready(address):
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(2)
            self.sock.connect(str(address))
    for _ in range(20):
        connection = Connection("localhost", timeout=2)
        try:
            connection.request("GET", "/status")
            response = connection.getresponse()
            state = json.loads(response.read(4097))
            if (response.status == 200 and state.get("persistent") is True
                    and state.get("applied") is True and not state.get("error")):
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.2)
    raise ValueError("服务已安装但规则尚未通过核验，请检查 systemd 服务日志")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--work-dir", default="/home/daniel/mskplt")
    result.add_argument("--uid", type=int, default=10001)
    result.add_argument("--port", type=int, default=50000)
    result.add_argument("--external-interface", default="enp7s0")
    result.add_argument("--bridge-interface", default="docker0")
    result.add_argument("--mss", type=int, default=1240)
    result.add_argument("--enable", action="store_true", help="首次安装时启用，已有设置保持不变")
    return result


if __name__ == "__main__":
    try:
        install(parser().parse_args())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("安装网络管理服务失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)

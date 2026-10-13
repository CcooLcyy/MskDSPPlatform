#!/usr/bin/env python3
"""从宿主机检查已启动服务的 HTTP 接口与 gRPC TCP 端口，仅使用标准库。"""

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request


def check(http_url: str, grpc_host: str, grpc_port: int, expected: dict) -> None:
    """验证接口契约；TCP 检查仅说明端口可连接，不代表 gRPC 业务互通。"""
    bodies = {}
    # 回环检查不经过宿主机代理，避免代理响应掩盖服务故障。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/healthz", "/readyz", "/api/v1/version"):
        with opener.open(http_url.rstrip("/") + path, timeout=2) as response:
            if response.status != 200:
                raise ValueError(f"{path} 状态码异常：{response.status}")
            body = response.read()
            if path != "/healthz":
                bodies[path] = json.loads(body)

    ready = bodies["/readyz"]
    if not isinstance(ready, dict) or ready.get("status") != "ready":
        raise ValueError(f"就绪接口响应异常：{ready!r}")
    agents = ready.get("online_agents")
    if type(agents) is not int or agents < 0:
        raise ValueError(f"在线数量字段异常：{agents!r}")

    version = bodies["/api/v1/version"]
    if not isinstance(version, dict) or version.get("service") != "mskdsp-platform":
        raise ValueError(f"版本接口服务标识异常：{version!r}")
    if version.get("protocol_version") != "platform.v1":
        raise ValueError(f"版本接口协议标识异常：{version!r}")
    for field, value in expected.items():
        if value is not None and version.get(field) != value:
            raise ValueError(f"版本接口 {field} 字段异常：预期 {value!r}，实际 {version.get(field)!r}")

    with socket.create_connection((grpc_host, grpc_port), timeout=2):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default="http://127.0.0.1:8080", help="HTTP 服务地址")
    parser.add_argument("--grpc-host", default="127.0.0.1", help="gRPC 服务主机")
    parser.add_argument("--grpc-port", default=50000, type=int, help="gRPC 服务端口")
    parser.add_argument("--timeout", default=45.0, type=float, help="等待服务就绪的秒数")
    parser.add_argument("--expected-version", help="预期构建版本")
    parser.add_argument("--expected-channel", help="预期发布通道")
    parser.add_argument("--expected-commit", help="预期源码提交号")
    parser.add_argument("--expected-build-time", help="预期构建时间")
    args = parser.parse_args()
    if args.timeout <= 0 or not 1 <= args.grpc_port <= 65535:
        parser.error("等待秒数必须大于零，端口必须在 1 至 65535 之间")

    deadline = time.monotonic() + args.timeout
    expected = {
        "version": args.expected_version,
        "channel": args.expected_channel,
        "commit": args.expected_commit,
        "build_time": args.expected_build_time,
    }
    while True:
        try:
            check(args.http_url, args.grpc_host, args.grpc_port, expected)
            print("检查通过：HTTP 三个接口响应正常，gRPC TCP 端口可连接。")
            return 0
        except (OSError, ValueError, urllib.error.URLError) as error:
            if time.monotonic() >= deadline:
                print(f"容器启动检查失败：{error}", file=sys.stderr)
                return 1
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())

"""生成 CI Python gRPC 绑定，并以无副作用的目录查询验证连接。"""
import argparse
from contextlib import contextmanager
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class ClientError(RuntimeError):
    pass


def require_grpc():
    if not all(importlib.util.find_spec(name) for name in ("grpc", "grpc_tools")):
        raise ClientError("缺少 Python gRPC 依赖，请使用包含 grpcio 和 grpcio-tools 的工具镜像")


def generate_bindings(proto_root, output_dir):
    protocol = Path(proto_root).resolve() / "platform/v1/software.proto"
    if not protocol.is_file():
        raise ClientError("缺少软件仓库协议，请检查平台协议子模块及父仓库固定提交")
    require_grpc()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # 使用协议所在目录作为编译根，避免遮蔽 Python 标准库 platform。
    command = [sys.executable, "-m", "grpc_tools.protoc", f"-I{protocol.parent}",
               f"--python_out={output}", f"--grpc_python_out={output}", protocol.name]
    result = subprocess.run(command, cwd=protocol.parent, capture_output=True, check=False)
    if result.returncode or not all((output / name).is_file() for name in
                                   ("software_pb2.py", "software_pb2_grpc.py")):
        raise ClientError("编译软件仓库 gRPC 协议失败，请核对协议内容和工具版本")
    return output


@contextmanager
def load_bindings(directory):
    # 临时加载后恢复模块，支持相同协议重复导入；不同协议需另起进程。
    names = ("software_pb2", "software_pb2_grpc")
    previous = {name: sys.modules.pop(name, None) for name in names}
    path = str(directory)
    sys.path.insert(0, path)
    importlib.invalidate_caches()
    try:
        yield tuple(importlib.import_module(name) for name in names)
    finally:
        sys.path.remove(path)
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


def probe(proto_root, target, *, plaintext=False, ca_file=None, timeout=10):
    if plaintext and ca_file:
        raise ClientError("明文连接不能配置 TLS CA 文件")
    if not 0 < timeout <= 60:
        raise ClientError("探测超时必须大于 0 且不超过 60 秒")
    require_grpc()
    import grpc

    with tempfile.TemporaryDirectory(prefix="mskdsp-grpc-") as temporary:
        output = generate_bindings(proto_root, temporary)
        with load_bindings(output) as (messages, services):
            if plaintext:
                channel = grpc.insecure_channel(target)
            else:
                roots = Path(ca_file).read_bytes() if ca_file else None
                channel = grpc.secure_channel(target, grpc.ssl_channel_credentials(roots))
            with channel:
                ready = grpc.channel_ready_future(channel)
                try:
                    ready.result(timeout=timeout)
                except grpc.FutureTimeoutError:
                    raise ClientError("gRPC 通道就绪超时，请检查地址、网络和 TLS/明文配置") from None
                finally:
                    ready.cancel()
                stub = services.SoftwareCatalogServiceStub(channel)
                try:
                    response = stub.ListPackages(messages.ListPackagesRequest(product="mskdsp-platform"),
                                                 timeout=timeout)
                except grpc.RpcError as error:
                    code = error.code().name
                    if code == "UNIMPLEMENTED":
                        raise ClientError("gRPC 通道已就绪，但目录接口返回 UNIMPLEMENTED，尚不能确认软件仓库已部署") from None
                    raise ClientError(f"gRPC 通道已就绪，但只读目录查询失败：{code}") from None
                return {"rpc": "SoftwareCatalogService.ListPackages", "package_count": len(response.packages)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="从固定协议生成 Python gRPC 客户端")
    generate.add_argument("--proto-root", type=Path, default=ROOT / "platform-proto")
    generate.add_argument("--output-dir", type=Path, required=True)
    check = commands.add_parser("probe", help="只读检查通道和软件目录接口，不携带发布令牌")
    check.add_argument("--proto-root", type=Path, default=ROOT / "platform-proto")
    check.add_argument("--target", required=True)
    check.add_argument("--plaintext", action="store_true", help="显式使用无 TLS 的入口")
    check.add_argument("--ca-file", type=Path)
    check.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            output = generate_bindings(args.proto_root, args.output_dir)
            print(f"平台 gRPC 客户端生成完成：{output}")
        else:
            result = probe(args.proto_root, args.target, plaintext=args.plaintext,
                           ca_file=args.ca_file, timeout=args.timeout)
            print("平台 gRPC 只读检查成功：" + json.dumps(result, ensure_ascii=False))
    except (ClientError, OSError, ImportError) as error:
        # 不回显 gRPC 详细错误或底层异常，避免暴露服务端内部信息。
        message = str(error) if isinstance(error, ClientError) else "请检查本地依赖、CA 文件和生成目录"
        print("平台 gRPC 客户端失败：" + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

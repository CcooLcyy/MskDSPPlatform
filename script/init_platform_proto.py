"""从 GitHub 公开仓库检出父仓库固定的平台协议，不使用旧 GitLab 读取凭据。"""
import os
from pathlib import Path
import subprocess
import sys


def initialize(root):
    root = Path(root)
    environment = os.environ.copy()
    environment.pop("PLATFORM_PROTO_READ_TOKEN", None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    print("从 GitHub 公开仓库初始化固定版本的平台协议，无需读取令牌。")
    subprocess.run(["git", "submodule", "sync", "--", "platform-proto"],
                   cwd=root, env=environment, check=True)
    subprocess.run(["git", "submodule", "update", "--init", "--recursive", "--", "platform-proto"],
                   cwd=root, env=environment, check=True)
    if not (root / "platform-proto/platform/v1/software.proto").is_file():
        raise RuntimeError("平台协议子模块缺少软件仓库协议，请核对父仓库固定的协议提交")
    if not (root / "platform-proto/platform/v1/templates.proto").is_file():
        raise RuntimeError("平台协议子模块缺少点表目录协议，请核对父仓库固定的协议提交")


if __name__ == "__main__":
    try:
        initialize(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        print("初始化平台协议失败：请检查 GitHub 网络可达性和子模块固定提交。", file=sys.stderr)
        raise SystemExit(1)

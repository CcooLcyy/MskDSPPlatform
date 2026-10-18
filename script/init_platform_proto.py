"""检出父仓库固定的私有平台协议，不将发布凭据写入 Git 配置或命令参数。"""
import base64
import os
from pathlib import Path
import subprocess
import sys


def initialize(root):
    root = Path(root)
    environment = os.environ.copy()
    token = environment.pop("PLATFORM_PROTO_READ_TOKEN", "")
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    if token:
        index = int(environment.get("GIT_CONFIG_COUNT", "0"))
        credential = base64.b64encode(("oauth2:" + token).encode("utf-8")).decode("ascii")
        environment[f"GIT_CONFIG_KEY_{index}"] = "http.http://192.168.10.198:8929/.extraHeader"
        environment[f"GIT_CONFIG_VALUE_{index}"] = "Authorization: Basic " + credential
        environment[f"GIT_CONFIG_KEY_{index + 1}"] = "credential.helper"
        environment[f"GIT_CONFIG_VALUE_{index + 1}"] = ""
        environment["GIT_CONFIG_COUNT"] = str(index + 2)
    subprocess.run(["git", "submodule", "update", "--init", "--recursive", "--", "platform-proto"],
                   cwd=root, env=environment, check=True)
    if not (root / "platform-proto/platform/v1/software.proto").is_file():
        raise RuntimeError("平台协议子模块缺少软件仓库协议，请核对父仓库固定的协议提交")


if __name__ == "__main__":
    try:
        initialize(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        print("初始化平台协议失败：请检查 GitLab 网络可达性、读取权限和子模块固定提交。", file=sys.stderr)
        raise SystemExit(1)

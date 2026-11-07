#!/usr/bin/env python3
"""冻结 GitHub main 的 CI 构建身份，并在上传前核对远端分支。"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    from script import release
except ModuleNotFoundError:
    import release


MAIN_REF = "refs/heads/main"


class CIError(ValueError):
    """可以安全输出的 CI 诊断。"""


def git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
        )
    except (OSError, subprocess.SubprocessError):
        raise CIError("Git 操作失败，请检查 GitHub 仓库和网络") from None
    return result.stdout.strip()


def main_context() -> tuple[str, str]:
    if (os.environ.get("GITHUB_EVENT_NAME") not in ("push", "workflow_dispatch")
            or os.environ.get("GITHUB_REF") != MAIN_REF):
        raise CIError("只有 main 分支的推送或手动工作流可以生成并上传 CI 程序包")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise CIError("GITHUB_REPOSITORY 格式不正确")
    commit = os.environ.get("GITHUB_SHA", "")
    if not release.SHA_RE.fullmatch(commit) or git("rev-parse", "HEAD") != commit:
        raise CIError("实际检出提交与 GITHUB_SHA 不一致")
    return repository, commit


def build_ci_metadata() -> dict:
    repository, commit = main_context()
    build_parts = (os.environ.get("GITHUB_RUN_NUMBER", ""), os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    if any(not re.fullmatch(r"[1-9][0-9]*", part) for part in build_parts):
        raise CIError("GITHUB_RUN_NUMBER 和 GITHUB_RUN_ATTEMPT 必须是正整数")
    version = release.read_version(Path("."))
    metadata = release.build_metadata("ci", version, commit, datetime.now(timezone.utc).isoformat(),
                                      ".".join(build_parts), repository)
    metadata["ref"] = "main"
    return metadata


def verify_head(data: dict) -> None:
    repository, commit = main_context()
    source = f"https://github.com/{repository}"
    if (not isinstance(data, dict) or data.get("channel") != "ci" or data.get("ref") != "main"
            or data.get("commit") != commit or data.get("source") != source):
        raise CIError("上传元数据与当前 GitHub main 工作流的冻结身份不一致")
    origin = git("remote", "get-url", "origin").lower()
    if origin not in (source.lower(), source.lower() + ".git"):
        raise CIError("origin 与当前 GitHub 仓库不一致")
    remote_main = git("ls-remote", "--exit-code", "origin", MAIN_REF).split()
    if len(remote_main) != 2 or remote_main[1] != MAIN_REF or not release.SHA_RE.fullmatch(remote_main[0]):
        raise CIError("GitHub 未返回有效的 main 分支身份")
    if remote_main[0] != commit:
        raise CIError("GitHub main 已发生变化，旧构建禁止上传，请使用最新工作流")
    print("上传前检查通过：冻结提交仍是 GitHub main 最新提交")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    metadata = commands.add_parser("metadata", help="生成 main CI 构建元数据")
    metadata.add_argument("--output", type=Path, default=Path("package/metadata.json"))
    metadata.add_argument("--github-output", type=Path)
    verify = commands.add_parser("verify-head", help="上传前核对 GitHub main 的最新提交")
    verify.add_argument("--metadata", type=Path, default=Path("package/metadata.json"))
    args = parser.parse_args(argv)
    if args.command == "verify-head":
        verify_head(json.loads(args.metadata.read_text(encoding="utf-8")))
        return
    data = build_ci_metadata()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.writelines(f"{key}={value}\n" for key, value in data.items())
    print(f"GitHub CI 构建身份已冻结：版本={data['version']} 提交={data['commit']}")


def cli() -> None:
    try:
        main()
    except CIError as error:
        raise SystemExit(f"GitHub CI 校验失败：{error}") from None
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit("GitHub CI 校验失败，请检查版本配置和冻结元数据文件") from None


if __name__ == "__main__":
    cli()

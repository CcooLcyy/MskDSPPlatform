#!/usr/bin/env python3
"""为 GitLab CI 生成冻结构建身份，并在发布前拒绝落后的 main 构建。"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

try:
    from script import publish_r2, release
except ModuleNotFoundError:
    import release
    import publish_r2

DEFAULT_IMAGE_REPOSITORY = "ghcr.io/ccoolcyy/mskdsp-platform"
DOTENV_FIELDS = {
    "PLATFORM_VERSION": "version", "PLATFORM_IMAGE_TAG": "image_tag",
    "PLATFORM_IMAGE_REPOSITORY": "image_repository", "PLATFORM_IMAGE": "image",
    "PLATFORM_ARTIFACT_NAME": "artifact_name", "PLATFORM_COMMIT": "commit",
    "PLATFORM_BUILD_TIME": "build_time",
}


class CIError(ValueError):
    """可安全显示给流水线用户的诊断，不含外部命令输出。"""


def git(*args: str, env: dict | None = None) -> str:
    # 不回显命令/标准错误；远端地址或 Git 的错误信息可能包含任务令牌。
    try:
        result = subprocess.run(["git", *args], check=True, capture_output=True,
                                text=True, encoding="utf-8", env=env)
    except (subprocess.CalledProcessError, OSError):
        raise CIError("Git 操作失败，请检查仓库访问权限和网络；为保护凭据已隐藏底层输出") from None
    return result.stdout.strip()


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        raise CIError("GitLab 项目地址必须是无凭据、无查询参数的 HTTP(S) 地址")
    return value.rstrip("/")


def build_ci_metadata(*, image_repository: str | None = None,
                      update_manifest_url: str | None = None) -> dict:
    env = os.environ
    if (env.get("CI_COMMIT_BRANCH") != "main" or env.get("CI_DEFAULT_BRANCH") != "main"
            or env.get("CI_COMMIT_TAG") or env.get("CI_MERGE_REQUEST_IID")
            or env.get("CI_PIPELINE_SOURCE") not in ("push", "web")):
        raise CIError("只有 main 分支的推送或手动流水线可以生成 CI 发布身份；合并请求只执行检查")
    commit = env.get("CI_COMMIT_SHA", "")
    if not release.SHA_RE.fullmatch(commit) or git("rev-parse", "HEAD") != commit:
        raise CIError("实际检出提交与 CI_COMMIT_SHA 不一致")
    build_parts = (env.get("CI_PIPELINE_IID", ""), env.get("CI_JOB_ID", ""))
    if any(not re.fullmatch(r"[1-9][0-9]*", part) for part in build_parts):
        raise CIError("CI_PIPELINE_IID 和 CI_JOB_ID 必须是正整数")
    source = public_url(env.get("CI_PROJECT_URL", ""))
    repository = image_repository or env.get("IMAGE_REPOSITORY") or DEFAULT_IMAGE_REPOSITORY
    # 完整仓库名称，不允许标签或摘要；registry:port 是合法的仓库前缀。
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+", repository):
        raise CIError("IMAGE_REPOSITORY 必须是完整的小写镜像仓库名，不含标签、摘要或凭据")
    version = release.read_version(Path("."))
    release.validate_source("ci", "main", version, [])
    # 复用现有版本/标签/产物命名规则，随后明确替换与托管平台有关的字段。
    data = release.build_metadata("ci", version, commit, datetime.now(timezone.utc).isoformat(),
                                  ".".join(build_parts), "ccoolcyy/MskDSPPlatform")
    data.update(source=source, image_repository=repository,
                image=f"{repository}:{data['image_tag']}", ref="main")
    if update_manifest_url:
        parsed = urlsplit(public_url(update_manifest_url))
        if parsed.scheme != "https" or parsed.path.split("/")[-2:] != ["ci", "latest.json"]:
            raise CIError("网页更新源必须是 CI 通道的 HTTPS latest.json")
        data["update_manifest_url"] = update_manifest_url
    return data


def _fetch_target(data: dict, remote: str | None) -> tuple[str, dict | None]:
    repository_url = os.environ.get("CI_REPOSITORY_URL")
    if not repository_url and os.environ.get("CI_SERVER_URL"):
        project = urlsplit(public_url(data["source"]))
        server = urlsplit(public_url(os.environ["CI_SERVER_URL"]))
        repository_url = urlunsplit((server.scheme, server.netloc, project.path + ".git", "", ""))
    if repository_url:
        parsed = urlsplit(repository_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query
                or parsed.fragment or any(c.isspace() for c in repository_url)):
            raise CIError("CI 仓库拉取地址格式不正确")
        host = parsed.netloc.rsplit("@", 1)[-1]
        target = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        env = os.environ.copy()
        username = unquote(parsed.username or "gitlab-ci-token")
        password = unquote(parsed.password or os.environ.get("CI_JOB_TOKEN", ""))
        if password:
            auth = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            env.update({"GIT_CONFIG_COUNT": str(count + 1),
                        f"GIT_CONFIG_KEY_{count}": f"http.{target}.extraheader",
                        f"GIT_CONFIG_VALUE_{count}": "Authorization: Basic " + auth})
        return target, env
    if remote:
        if remote not in ("gitlab", "origin"):
            raise CIError("发布检查只允许 gitlab 或 origin 远端")
        return remote, None
    remotes = git("remote").splitlines()
    if "gitlab" in remotes:
        return "gitlab", None
    if "origin" in remotes:
        return "origin", None
    raise CIError("未找到 GitLab 拉取地址或 gitlab/origin 远端")


def publish_head_check(data: dict, remote: str | None = None) -> None:
    if data.get("channel") != "ci" or data.get("ref") != "main" or not release.SHA_RE.fullmatch(data.get("commit", "")):
        raise CIError("发布检查需要 main CI 的冻结元数据")
    target, env = _fetch_target(data, remote)
    tracking = f"refs/remotes/{target}/main" if target in ("gitlab", "origin") else "refs/gitlab-ci/publish-main"
    git("fetch", "--no-tags", target, f"+refs/heads/main:{tracking}", env=env)
    if git("rev-parse", tracking) != data["commit"]:
        raise CIError("当前 main 已发生变化：旧构建禁止更新 latest.json，请使用最新流水线")
    print("发布前检查通过：冻结提交仍是 main 最新提交")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    metadata = commands.add_parser("metadata", help="生成 main CI 构建元数据和 dotenv")
    metadata.add_argument("--output", type=Path, default=Path("package/metadata.json"))
    metadata.add_argument("--dotenv", type=Path, default=Path("package/build.env"))
    metadata.add_argument("--image-repository")
    metadata.add_argument("--update-manifest-url")
    publish = commands.add_parser("publish-head-check", help="拒绝旧构建覆盖通道清单")
    publish.add_argument("--metadata", type=Path, default=Path("package/metadata.json"))
    publish.add_argument("--remote", choices=("gitlab", "origin"))
    r2 = commands.add_parser("publish-r2", help="校验 main，上传资产，并在提交入口前再次检查 main")
    r2.add_argument("--metadata", type=Path, default=Path("package/metadata.json"))
    r2.add_argument("--output-dir", type=Path, required=True)
    r2.add_argument("--bucket", required=True)
    r2.add_argument("--public-base-url", required=True)
    r2.add_argument("--prefix", default="mskdsp-platform")
    args = parser.parse_args(argv)
    if args.command == "publish-r2":
        data = json.loads(args.metadata.read_text(encoding="utf-8"))
        publish_head_check(data)
        publish_r2.publish(output_dir=args.output_dir, bucket=args.bucket,
                           public_base_url=args.public_base_url, prefix=args.prefix,
                           before_latest=lambda: publish_head_check(data))
        return
    if args.command == "publish-head-check":
        publish_head_check(json.loads(args.metadata.read_text(encoding="utf-8")), args.remote)
        return
    data = build_ci_metadata(image_repository=args.image_repository,
                             update_manifest_url=args.update_manifest_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.dotenv.parent.mkdir(parents=True, exist_ok=True)
    args.dotenv.write_text("".join(f"{name}={data[key]}\n" for name, key in DOTENV_FIELDS.items()), encoding="utf-8")
    print(f"CI 构建身份已冻结：版本={data['version']} 提交={data['commit']}")


def cli(argv: list[str] | None = None) -> None:
    try:
        main(argv)
    except CIError as error:
        raise SystemExit(f"GitLab CI 校验失败：{error}") from None
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit("GitLab CI 校验失败，请检查来源、配置、权限和网络；未输出可能包含凭据的底层错误") from None


if __name__ == "__main__":
    cli()

#!/bin/sh
# 在每台 Linux x64 Runner 宿主机预加载工具镜像；更新工具时使用新的版本标签。
set -eu
cd "$(dirname "$0")"
verify_image=${PLATFORM_VERIFY_IMAGE:-mskdsp-platform-ci-verify:rust1.94.1-v1}
tools_image=${PLATFORM_TOOLS_IMAGE:-mskdsp-platform-ci-tools:bookworm-v1}
docker build --platform linux/amd64 --target verify -t "$verify_image" .
docker build --platform linux/amd64 --target tools -t "$tools_image" .
docker image inspect --format '{{.Id}} {{join .RepoTags ","}}' "$verify_image" "$tools_image"

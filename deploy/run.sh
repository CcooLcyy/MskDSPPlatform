#!/bin/sh
# 从已校验的离线交付目录启动平台容器，不使用 Compose。
set -eu
task_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if [ -f "$task_dir/.env" ]; then
  # .env 是发布工具生成且已校验的唯一镜像身份文件。
  . "$task_dir/.env"
fi
image=${MSKDSP_PLATFORM_IMAGE:-ghcr.io/ccoolcyy/mskdsp-platform:ci}
docker volume create mskdsp-platform-runtime >/dev/null
docker run -d --name mskdsp-platform --init --restart unless-stopped --stop-timeout 30 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -p "127.0.0.1:${MSKDSP_HTTP_PORT:-8080}:8080" \
  -p "127.0.0.1:${MSKDSP_GRPC_PORT:-50051}:50051" \
  --mount type=volume,source=mskdsp-platform-runtime,target=/var/lib/mskdsp-platform \
  --mount "type=bind,source=$task_dir/deploy/config.toml,target=/app/config.toml,readonly" \
  --mount "type=bind,source=$task_dir/deploy/updater.json,target=/app/updater.json,readonly" \
  "$image"

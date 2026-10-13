#!/bin/sh
# 从已校验的离线交付目录启动平台容器，不使用 Compose。
set -eu
task_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if [ -f "$task_dir/.env" ]; then
  # .env 是发布工具生成且已校验的唯一镜像身份文件。
  . "$task_dir/.env"
fi
image=${MSKDSP_PLATFORM_IMAGE:-ghcr.io/ccoolcyy/mskdsp-platform:ci}
work_dir=${MSKDSP_WORK_DIR:-/home/daniel/mskplt}
case "$work_dir" in
  /*) ;;
  *) echo "平台工作目录必须是绝对路径" >&2; exit 1 ;;
esac
if [ "$work_dir" = / ]; then
  echo "不能使用宿主机根目录作为平台工作目录" >&2
  exit 1
fi
mkdir -p "$work_dir"
work_dir=$(CDPATH='' cd -- "$work_dir" && pwd -P)
if [ "$work_dir" = / ]; then
  echo "不能使用宿主机根目录作为平台工作目录" >&2
  exit 1
fi
mkdir -p "$work_dir/deploy" "$work_dir/runtime/data" "$work_dir/runtime/logs" "$work_dir/runtime/tmp"
for config in config.toml updater.json; do
  if [ ! -e "$work_dir/deploy/$config" ]; then
    cp "$task_dir/deploy/$config" "$work_dir/deploy/$config"
    chmod 0644 "$work_dir/deploy/$config"
  fi
done
# 仅准备平台目录的属主，不递归改动已有业务文件。
docker run --rm --network none --read-only --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE \
  --user 0:0 --entrypoint chown \
  --mount "type=bind,source=$work_dir/runtime,target=/var/lib/mskdsp-platform" \
  "$image" 10001:10001 /var/lib/mskdsp-platform /var/lib/mskdsp-platform/data \
  /var/lib/mskdsp-platform/logs /var/lib/mskdsp-platform/tmp
echo "平台工作目录：$work_dir"
docker run -d --name mskdsp-platform --init --restart unless-stopped --stop-timeout 30 \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --workdir /var/lib/mskdsp-platform \
  --env MSKDSP_RUNTIME_DIR=/var/lib/mskdsp-platform \
  --env MSKDSP_PLATFORM_DATA_DIR=/var/lib/mskdsp-platform/data \
  --env TMPDIR=/var/lib/mskdsp-platform/tmp \
  -p "${MSKDSP_BIND_ADDRESS:-127.0.0.1}:${MSKDSP_HTTP_PORT:-8080}:8080" \
  -p "${MSKDSP_BIND_ADDRESS:-127.0.0.1}:${MSKDSP_GRPC_PORT:-50000}:50000" \
  --mount "type=bind,source=$work_dir/runtime,target=/var/lib/mskdsp-platform" \
  --mount "type=bind,source=$work_dir/runtime/tmp,target=/tmp" \
  --mount "type=bind,source=$work_dir/deploy/config.toml,target=/app/config.toml,readonly" \
  --mount "type=bind,source=$work_dir/deploy/updater.json,target=/app/updater.json,readonly" \
  "$image"

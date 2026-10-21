#!/usr/bin/env bash
# 检查作业直接复用持久 Cargo 缓存，保留原有编译和测试门禁。
set -euo pipefail

log() { printf '[platform-ci] %s\n' "$*"; }
fail() { log "错误：$*" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${ROOT}"
mkdir -p package/logs .ci-tmp
export TMPDIR="${ROOT}/.ci-tmp"

cache_mount="${PLATFORM_CACHE_MOUNT:-}"
if [[ -n "${cache_mount}" ]]; then
  [[ "${cache_mount}" == /* ]] || fail '平台缓存挂载目录必须为绝对路径'
  cache_mount="$(realpath -m -- "${cache_mount}")"
  mountpoint -q -- "${cache_mount}" || fail "平台持久缓存挂载不存在：${cache_mount}"
fi

prepare_cache_path() {
  local configured="$1" label="$2" resolved
  [[ "${configured}" == /* ]] || fail "${label}必须为绝对路径"
  resolved="$(realpath -m -- "${configured}")"
  if [[ -n "${cache_mount}" ]]; then
    case "${resolved}/" in
      "${ROOT}/"*) fail "${label}不能位于项目工作区内" ;;
    esac
    case "${resolved}/" in
      "${cache_mount%/}/"*) ;;
      *) fail "${label}解析后不在指定挂载内" ;;
    esac
  fi
  printf '%s' "${resolved}"
}

cargo_home="$(prepare_cache_path "${CARGO_HOME:-${ROOT}/.cargo}" 'Cargo 缓存目录')"
cargo_target="$(prepare_cache_path "${CARGO_TARGET_DIR:-${ROOT}/target}" 'Cargo 构建缓存目录')"
export CARGO_HOME="${cargo_home}" CARGO_TARGET_DIR="${cargo_target}"
mkdir -p "${CARGO_HOME}" "${CARGO_TARGET_DIR}"
log "Cargo 缓存目录：${CARGO_HOME}"
log "Cargo 构建缓存目录：${CARGO_TARGET_DIR}"
if [[ -x "${CARGO_TARGET_DIR}/debug/mskdsp-platform" ]]; then
  log '发现已有平台程序缓存，由 Cargo 指纹检查决定复用或重新编译'
else
  log '尚无平台程序缓存，按原规则准备完整构建'
fi

python3 -B script/init_platform_proto.py
cargo fmt --all -- --check
cargo build --locked 2>&1 | tee package/logs/cargo-build.log
[[ -x "${CARGO_TARGET_DIR}/debug/mskdsp-platform" ]] || fail '平台调试程序未生成，拒绝跳过真实进程测试'
node --version
cargo test --locked --all-targets 2>&1 | tee package/logs/cargo-test.log
# 仅真实平台进程夹具的数据和 SQLite 使用有界 tmpfs，其他测试及日志沿用原目录。
MSKDSP_PLATFORM_TEST_TMPDIR=/dev/shm python3 -B -m unittest discover -s test -p 'test_*.py' -v \
  2>&1 | tee package/logs/python-test.log

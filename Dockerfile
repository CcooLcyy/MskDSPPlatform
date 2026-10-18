ARG RUST_IMAGE=rust:1.94.1-bookworm
ARG RUNTIME_IMAGE=debian:bookworm-slim
FROM ${RUST_IMAGE} AS builder

WORKDIR /build
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

COPY Cargo.toml Cargo.lock build.rs ./
COPY src/ src/
COPY proto/ proto/
COPY web/ web/
ARG VERSION=0.0.1
ARG CHANNEL=dev
ARG COMMIT=unknown
ARG BUILD_TIME=unknown
ENV MSKDSP_BUILD_VERSION=${VERSION} \
    MSKDSP_BUILD_CHANNEL=${CHANNEL} \
    MSKDSP_BUILD_COMMIT=${COMMIT} \
    MSKDSP_BUILD_TIME=${BUILD_TIME}
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/target \
    cargo build --locked --release \
    && cp target/release/mskdsp-platform /build/mskdsp-platform

FROM ${RUNTIME_IMAGE} AS runtime

RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends ca-certificates curl libgcc-s1 python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin platform \
    && mkdir -p /var/lib/mskdsp-platform/releases /var/lib/mskdsp-platform/data \
        /var/lib/mskdsp-platform/logs /var/lib/mskdsp-platform/tmp \
    && touch /var/lib/mskdsp-platform/.keep \
    && chown -R 10001:10001 /var/lib/mskdsp-platform

ARG VERSION=0.0.1
ARG CHANNEL=dev
ARG COMMIT=unknown
ARG BUILD_TIME=unknown
ARG SOURCE
ARG UPDATE_MANIFEST_URL=https://update.clsclear.top/mskdsp-platform/ci/latest.json
LABEL org.opencontainers.image.title="MskDSPPlatform" \
    org.opencontainers.image.description="MskDSP 远程服务平台基础服务" \
    org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.revision="${COMMIT}" \
    org.opencontainers.image.created="${BUILD_TIME}" \
    org.opencontainers.image.source="${SOURCE}" \
    io.mskdsp.channel="${CHANNEL}"

WORKDIR /var/lib/mskdsp-platform
COPY --from=builder /build/mskdsp-platform /usr/local/bin/mskdsp-platform
COPY deploy/config.toml /app/config.toml
COPY script/runtime.py script/update_common.py /opt/mskdsp-manager/
RUN UPDATE_MANIFEST_URL="${UPDATE_MANIFEST_URL}" python3 -c 'import json,os; open("/app/updater.json","w").write(json.dumps({"manifest_url":os.environ["UPDATE_MANIFEST_URL"]})+"\n")'
ENV MSKDSP_PLATFORM_CONFIG=/app/config.toml \
    MSKDSP_UPDATER_CONFIG=/app/updater.json \
    MSKDSP_RUNTIME_DIR=/var/lib/mskdsp-platform \
    MSKDSP_PLATFORM_DATA_DIR=/var/lib/mskdsp-platform/data \
    TMPDIR=/var/lib/mskdsp-platform/tmp \
    MSKDSP_MANAGER_LISTEN=0.0.0.0:8080 \
    MSKDSP_CHILD_HTTP_ADDR=127.0.0.1:8081 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RUST_LOG=info
USER 10001:10001
VOLUME ["/var/lib/mskdsp-platform"]
EXPOSE 8080 50000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["python3", "/opt/mskdsp-manager/runtime.py"]

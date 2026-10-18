ARG RUST_IMAGE=docker.m.daocloud.io/library/rust:1.94.1-bookworm@sha256:6ae102bdbf528294bc79ad6e1fae682f6f7c2a6e6621506ba959f9685b308a55
ARG TOOLS_BASE_IMAGE=docker.m.daocloud.io/library/debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

FROM ${RUST_IMAGE} AS verify
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends python3 python3-grpcio python3-grpc-tools python3-protobuf nodejs protobuf-compiler ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rustup component add rustfmt --toolchain 1.94.1
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends python3-pkg-resources \
    && rm -rf /var/lib/apt/lists/*
RUN rustc --version && cargo --version && cargo fmt --version \
    && python3 --version && node --version && protoc --version && git --version \
    && python3 -B -c "import grpc, grpc_tools.protoc, google.protobuf; print('Python gRPC imports OK')"

FROM ${TOOLS_BASE_IMAGE} AS tools
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends python3 python3-grpcio python3-grpc-tools python3-protobuf git awscli ca-certificates libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 install -y --no-install-recommends python3-pkg-resources \
    && rm -rf /var/lib/apt/lists/*
RUN python3 --version && git --version && aws --version \
    && python3 -B -c "import grpc, grpc_tools.protoc, google.protobuf; print('Python gRPC imports OK')"

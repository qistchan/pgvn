FROM ubuntu:24.04
# Build tools only. Product APR/APU/SVN/libpq come from linux-dependencies.json.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -s /run/secrets/proxy_ca ]; then \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
        build-essential autoconf automake libtool libtool-bin pkg-config python3 \
        bison flex libssl-dev libexpat1-dev libsqlite3-dev zlib1g-dev \
        liblz4-dev libutf8proc-dev ca-certificates; \
    else \
      apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool libtool-bin pkg-config python3 \
        bison flex libssl-dev libexpat1-dev libsqlite3-dev zlib1g-dev \
        liblz4-dev libutf8proc-dev ca-certificates; \
    fi \
    && rm -rf /var/lib/apt/lists/*
# Git reads the shared source identity; patchelf is used by later relocatable
# packaging work, not to select a different system SVN installation.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -s /run/secrets/proxy_ca ]; then \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends git patchelf; \
    else \
      apt-get update && apt-get install -y --no-install-recommends git patchelf; \
    fi \
    && rm -rf /var/lib/apt/lists/*

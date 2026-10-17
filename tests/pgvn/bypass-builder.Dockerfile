FROM ubuntu:24.04
# Component-test toolchain only; this is not a PgVN product image or SVN SDK.
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
       gcc libc6-dev pkg-config libapr1-dev libaprutil1-dev libsvn-dev libpq-dev nodejs python3 \
    && rm -rf /var/lib/apt/lists/*

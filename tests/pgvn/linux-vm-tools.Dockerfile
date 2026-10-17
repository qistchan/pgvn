FROM ubuntu:24.04
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
       qemu-system-x86 qemu-utils e2fsprogs python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM pgvn-linux-product-builder:ubuntu24 AS postgres-build
COPY postgresql.tar.gz linux-dependencies.json /inputs/
RUN python3 -c 'import hashlib,json,pathlib; p=pathlib.Path("/inputs"); s=next(x for x in json.loads((p/"linux-dependencies.json").read_text())["sources"] if x["name"]=="postgresql"); assert hashlib.sha256((p/"postgresql.tar.gz").read_bytes()).hexdigest()==s["sha256"]' \
    && mkdir /postgres-source \
    && tar -xf /inputs/postgresql.tar.gz --strip-components=1 -C /postgres-source
WORKDIR /postgres-source
RUN ./configure --prefix=/opt/postgresql --without-icu --without-readline --without-zlib --with-openssl \
    && make -j4 && make install

FROM ubuntu:24.04
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
       systemd systemd-sysv dbus udev linux-image-generic initramfs-tools busybox \
       python3 subversion iproute2 openssl ca-certificates libevent-2.1-7t64 libssl3t64 libpcre2-8-0 \
       libuuid1 libexpat1 libcrypt1 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --user-group --home-dir /var/lib/pgsql --shell /usr/sbin/nologin postgres \
    && useradd --system --user-group --home-dir /var/lib/pgvn --shell /usr/sbin/nologin pgvn
COPY --from=postgres-build /opt/postgresql /opt/postgresql

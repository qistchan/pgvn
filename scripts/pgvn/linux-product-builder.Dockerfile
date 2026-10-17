FROM pgvn-linux-builder:ubuntu24
# Product HTTP adapter, Apache build prerequisites and independent clients.
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
       libevent-dev libpcre2-dev openssl curl subversion openjdk-17-jdk-headless libsvnkit-java \
    && rm -rf /var/lib/apt/lists/*

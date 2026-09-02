# Dockerfile para compilar TimescaleDB desde codigo fuente sobre PostgreSQL riscv64
# Uso: docker build -f Dockerfile.timescaledb-riscv64 -t timescaledb-riscv64:pg17 .
#
# Nota de version: usamos PG17 (no PG16) porque en riscv64 el paquete
# postgresql-server-dev-16 no existe -- PGDG no publica binarios .deb para
# esta arquitectura. PG17 es el nativo de Debian Trixie e incluye el paquete
# de desarrollo completo para riscv64. TimescaleDB 2.19.3 soporta PG17.

FROM postgres:17-trixie

# Version de TimescaleDB a compilar (ajusta segun necesites)
ARG TS_VERSION=2.19.3

# Dependencias de compilacion.
# Nota: la imagen base trae libpq5 compilado desde el paquete fuente de PGDG
# (version "17.11-1.pgdg13+2"), pero PGDG no publica binarios para riscv64,
# asi que postgresql-server-dev-17 (que SI viene de Debian nativo) requiere
# la version de Debian de libpq5/libpq-dev ("17.11-0+deb13u1"). Segun las
# reglas de dpkg esto cuenta como un downgrade, por lo que hay que forzarlo
# explicitamente fijando ambos paquetes a la misma version de Debian.
RUN apt-get update && apt-get install -y --allow-downgrades \
    build-essential \
    cmake \
    git \
    libssl-dev \
    libkrb5-dev \
    libpq5=17.11-0+deb13u1 \
    libpq-dev=17.11-0+deb13u1 \
    postgresql-server-dev-17 \
    && rm -rf /var/lib/apt/lists/*

# Clona y compila TimescaleDB COMPLETO (Apache-2 + Community/TSL).
# La Community Edition (TSL) incluye compresion y continuous aggregates.
# Es gratis para auto-hospedar; solo restringe ofrecerla como servicio administrado a terceros.
# NO uses -DAPACHE_ONLY=1 aqui o perderias compresion/agregados continuos.
RUN git clone https://github.com/timescale/timescaledb /usr/src/timescaledb \
    && cd /usr/src/timescaledb \
    && git checkout ${TS_VERSION} \
    && ./bootstrap -DCMAKE_BUILD_TYPE=Release \
    && cd build \
    && make -j$(nproc) \
    && make install \
    && cd / \
    && rm -rf /usr/src/timescaledb

# Nota: no tocamos postgresql.conf.sample aqui porque el proyecto monta su
# propio db/postgresql.conf via docker-compose, que ya define
# shared_preload_libraries = 'timescaledb'.

# Script de inicializacion: crea la extension automaticamente en la DB por defecto
RUN mkdir -p /docker-entrypoint-initdb.d
RUN echo "CREATE EXTENSION IF NOT EXISTS timescaledb;" > /docker-entrypoint-initdb.d/000_timescaledb.sql

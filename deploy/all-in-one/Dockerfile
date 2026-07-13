FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    bash \
    supervisor \
    redis-server \
    postgresql \
    postgresql-client \
    postgresql-postgis \
    postgresql-postgis-scripts \
    openjdk-21-jre-headless \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    libpq-dev \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN curl -fsSL https://dl.min.io/server/minio/release/linux-amd64/minio -o /usr/local/bin/minio \
    && chmod +x /usr/local/bin/minio
RUN curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc \
    && chmod +x /usr/local/bin/mc

RUN curl -fsSL https://github.com/keycloak/keycloak/releases/download/26.0.5/keycloak-26.0.5.tar.gz -o keycloak.tgz \
    && mkdir -p /opt/keycloak \
    && tar -xzf keycloak.tgz -C /opt \
    && mv /opt/keycloak-26.0.5/* /opt/keycloak/ \
    && rm -rf keycloak.tgz /opt/keycloak-26.0.5

WORKDIR /code
COPY ./applications/cm-techmap-backend /code
RUN pip install --upgrade pip && pip install .
COPY ./applications/docker/keycloak /opt/keycloak/data/import

COPY ./applications/deploy/all-in-one/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY ./applications/deploy/all-in-one/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000 8080 9000 9001 5555 6379 5432

ENV POSTGRES_DB=cm_techmap \
    POSTGRES_USER=cm_techmap \
    POSTGRES_PASSWORD=cm_techmap_dev_2026 \
    KC_POSTGRES_DB=keycloak \
    MINIO_ROOT_USER=cm_techmap_admin \
    MINIO_ROOT_PASSWORD=cm_techmap_minio_2026 \
    KEYCLOAK_ADMIN_USERNAME=admin \
    KEYCLOAK_ADMIN_PASSWORD=admin_dev_2026 \
    FLOWER_BASIC_AUTH=admin:flower_dev_2026

ENTRYPOINT ["/entrypoint.sh"]

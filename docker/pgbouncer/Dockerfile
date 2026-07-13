# ==============================================================================
# CM TECHMAP — PgBouncer Container
# Lightweight connection pooler for PostgreSQL multi-tenant
# ==============================================================================

FROM alpine:3.20

RUN apk add --no-cache pgbouncer bash

# Create pgbouncer dirs
RUN mkdir -p /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer && \
    chown -R pgbouncer:pgbouncer /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer

COPY pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    chown pgbouncer:pgbouncer /etc/pgbouncer/pgbouncer.ini

EXPOSE 6432

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD pg_isready -h 127.0.0.1 -p 6432 || exit 1

ENTRYPOINT ["/entrypoint.sh"]

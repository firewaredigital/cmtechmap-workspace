# ==============================================================================
# CM TECHMAP — Backup Container
# Alpine-based container with pg_dump + MinIO client for automated backups
# ==============================================================================

FROM alpine:3.20

# Install PostgreSQL client + MinIO client + cron
RUN apk add --no-cache \
    postgresql16-client \
    curl \
    bash \
    tzdata \
    jq \
    && curl -o /usr/local/bin/mc https://dl.min.io/client/mc/release/linux-amd64/mc \
    && chmod +x /usr/local/bin/mc

# Set timezone (configurable via TZ env)
ENV TZ=America/Sao_Paulo

# Copy backup script
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

# Copy entrypoint (sets up cron schedule)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

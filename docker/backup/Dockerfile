# ==============================================================================
# CM TECHMAP — Backup Container
# Alpine-based container with pg_dump + MinIO client for automated backups
# ==============================================================================

FROM alpine:3.20

# Install PostgreSQL client + MinIO client + cron
# mc download is architecture-aware: the OCI Always Free VM is ARM (aarch64);
# a hardcoded amd64 binary fails there with "exec format error".
RUN apk add --no-cache \
    postgresql16-client \
    curl \
    bash \
    tzdata \
    jq \
    && case "$(uname -m)" in \
         aarch64|arm64) MC_ARCH="linux-arm64" ;; \
         *) MC_ARCH="linux-amd64" ;; \
       esac \
    && curl -fSL -o /usr/local/bin/mc "https://dl.min.io/client/mc/release/${MC_ARCH}/mc" \
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

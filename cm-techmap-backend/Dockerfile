# ==============================================================================
# CM TECHMAP Backend — Multi-stage Docker Build
# Python 3.12 + GDAL + Geospatial dependencies
# ==============================================================================

# ── Stage 1: Base with system dependencies ────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies for PostGIS, GDAL, geospatial processing, and PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    gdal-bin \
    curl \
    # WeasyPrint dependencies (PDF generation)
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Set GDAL environment for Python bindings
ENV GDAL_CONFIG=/usr/bin/gdal-config
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

WORKDIR /code

# ── Stage 2: Dependencies ────────────────────────────────────────────────────
FROM base AS deps

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir .

# ── Stage 3: Application ─────────────────────────────────────────────────────
FROM base AS app

COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY . /code/

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /code -s /sbin/nologin appuser \
    && chown -R appuser:appuser /code
USER appuser

EXPOSE 8000

# Default command (overridden in docker-compose for worker/flower)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the project itself
COPY mtg_collector/ mtg_collector/
RUN uv sync --frozen --no-dev

# Stage 2: Runtime
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy venv and app from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/mtg_collector /app/mtg_collector

# Copy OCR models
COPY models/ocr/ /app/models/ocr/

ENV PATH="/app/.venv/bin:$PATH"
ENV MTGC_HOME=/data
ENV EASYOCR_MODEL_STORAGE=/app/models/ocr

EXPOSE 8081

ENTRYPOINT ["mtg", "crack-pack-server", "--port", "8081", "--https"]

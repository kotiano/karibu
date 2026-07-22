# Karibu POS backend (FastAPI) — single portable production image.
#
# Works the same locally, from Docker Hub, and on any PaaS:
#   - binds $PORT (defaults to 8000) so hosts that inject a port just work
#   - runs `alembic upgrade head` on start, so the schema is always current
#
# Build:  docker build -t karibu-pos-api:latest ./backend
# Run:    docker run -p 8000:8000 --env-file backend/.env karibu-pos-api:latest
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Migrate, then serve. Uvicorn workers under Gunicorn's process manager: async
# workers handle the outbound M-Pesa calls without blocking.
# NOTE: with more than one instance, run the scheduler separately and set
# ENABLE_SCHEDULER=false here, or the billing sweep fires once per instance.
CMD alembic upgrade head && \
    gunicorn app.main:app \
      --worker-class uvicorn.workers.UvicornWorker \
      --workers 2 \
      --bind 0.0.0.0:${PORT:-8000} \
      --timeout 60
# Karibu POS backend (FastAPI) image.
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

# Uvicorn workers behind Gunicorn's process manager. Async workers handle the
# outbound M-Pesa calls efficiently. Scale replicas via the orchestrator, not
# just workers, for real horizontal load balancing.
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "3", "--bind", "0.0.0.0:8000", "--timeout", "60"]

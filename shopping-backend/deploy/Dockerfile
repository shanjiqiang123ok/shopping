# syntax=docker/dockerfile:1
# One image, two services. Built only once you have Docker — local development uses
# `scripts/run_dev.sh` / `scripts/run_demo.py` instead (no Docker, no MySQL required).
#
#   docker build -f deploy/Dockerfile --target business -t shopping-business .
#   docker build -f deploy/Dockerfile --target agent    -t shopping-agent .

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependency layer: the manifest changes far less often than the source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra mysql --extra llm

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

ENV PYTHONPATH=/app/src \
    APP_ENV=prod \
    SEED_ON_STARTUP=false \
    DATA_DIR=/data \
    LOG_DIR=/data/logs

RUN mkdir -p /data && chown -R nobody:nogroup /app /data
USER nobody

FROM base AS business
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import httpx,sys;sys.exit(0 if httpx.get('http://127.0.0.1:8001/health',timeout=2).status_code==200 else 1)"
CMD ["python", "-m", "uvicorn", "shopping_backend.business_api.main:app", "--host", "0.0.0.0", "--port", "8001"]

FROM base AS agent
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import httpx,sys;sys.exit(0 if httpx.get('http://127.0.0.1:8000/health',timeout=2).status_code==200 else 1)"
CMD ["python", "-m", "uvicorn", "shopping_backend.agent_api.main:app", "--host", "0.0.0.0", "--port", "8000"]

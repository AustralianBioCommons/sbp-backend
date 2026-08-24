# syntax=docker/dockerfile:1.4
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# git is required by app/services/workflow_repo_staging.py, which clones
# workflow pipeline repos (with real .git metadata, not just extracted source)
# to stage them onto Gadi via S3 + Globus - Gadi compute nodes have no network
# access, so Nextflow can't fetch/clone them itself at run time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies only (skip project build)
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Install the project itself
RUN uv pip install --no-deps -e .

COPY scripts ./scripts

EXPOSE 3000

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--forwarded-allow-ips", "*"]

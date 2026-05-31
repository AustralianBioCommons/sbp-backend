# syntax=docker/dockerfile:1.4
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

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

CMD ["uv", "run", "--no-sync", "python", "scripts/bootstrap.py", "serve"]

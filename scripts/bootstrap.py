"""Entrypoint for ECS tasks — run DB migrations and/or start the API server."""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import suppress

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

LOG = logging.getLogger("bootstrap")


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("BOOTSTRAP_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def wait_for_database(
    max_attempts: int = 30,
    delay_seconds: int = 5,
) -> None:
    database_url = os.environ["DATABASE_URL"]
    LOG.info("Waiting for database to become available...")

    for attempt in range(1, max_attempts + 1):
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            LOG.info("Database is ready")
            return
        except OperationalError as exc:
            engine.dispose()
            LOG.warning("Database not ready (attempt %s/%s): %s", attempt, max_attempts, exc)
            if attempt >= max_attempts:
                raise
            time.sleep(delay_seconds)
        except Exception:
            engine.dispose()
            LOG.exception("Unexpected error while checking database readiness")
            raise


def run_migrations() -> None:
    LOG.info("Running database migrations")
    config = Config(os.getenv("ALEMBIC_CONFIG", "alembic.ini"))
    command.upgrade(config, "head")
    LOG.info("Database migrations complete")


def start_server() -> None:
    import uvicorn

    host = os.getenv("UVICORN_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3000"))
    LOG.info("Starting uvicorn on %s:%s", host, port)

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def migrate() -> None:
    """Run migrations then exit — used by the migration ECS task."""
    _configure_logging()
    try:
        wait_for_database()
        run_migrations()
    except Exception:
        LOG.exception("Fatal error during migrate")
        sys.exit(1)


def serve() -> None:
    """Start the API server — migrations are assumed to have already run."""
    _configure_logging()
    try:
        wait_for_database()
        start_server()
    except Exception:
        LOG.exception("Fatal error during serve")
        sys.exit(1)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if mode == "migrate":
        migrate()
    elif mode == "serve":
        serve()
    else:
        print(f"Unknown mode: {mode!r}. Use 'migrate' or 'serve'.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()

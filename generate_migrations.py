#!/usr/bin/env python3
"""Spin up a temporary Postgres container and generate or check Alembic migrations."""

import os
import subprocess
import time

import click

DB_CONTAINER_NAME = "temp_alembic_db"
DEFAULT_POSTGRES_IMAGE = "postgres:17"
DEFAULT_PORT = 5433
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "postgres"
POSTGRES_DB = "sbp"


def run(cmd: str, env: dict | None = None) -> None:
    print(f"> {cmd}")
    subprocess.run(cmd, shell=True, check=True, env=env or os.environ)


def print_db_schema() -> None:
    print("Printing database schema...")
    query = (
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "ORDER BY table_name, ordinal_position;"
    )
    env = os.environ.copy()
    env["PGPASSWORD"] = POSTGRES_PASSWORD
    run(
        f'psql -h localhost -p {DEFAULT_PORT} -U {POSTGRES_USER} -d {POSTGRES_DB} -c "{query}"',
        env=env,
    )


@click.command()
@click.option("--revision-message", "-m", required=False, help="Message for the Alembic revision.")
@click.option("--check", is_flag=True, help="Only run 'alembic check' after the DB is ready.")
@click.option("--print-schema", is_flag=True, help="Print the schema after upgrade/check.")
@click.option(
    "--autogenerate/--no-autogenerate",
    default=True,
    help="Use --no-autogenerate to create a blank migration.",
)
def generate_migrations(
    revision_message: str | None, check: bool, print_schema: bool, autogenerate: bool
) -> None:
    """Spin up a temp Postgres DB, apply migrations or run alembic check, optionally print schema."""
    if not check and not revision_message:
        raise click.UsageError(
            "Missing option '-m' / '--revision-message'. Required unless using --check."
        )

    database_url = (
        f"postgresql+psycopg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@localhost:{DEFAULT_PORT}/{POSTGRES_DB}"
    )
    os.environ["DATABASE_URL"] = database_url

    try:
        print("Starting temporary Postgres container...")
        run(
            f"docker run --rm -d --name {DB_CONTAINER_NAME} "
            f"-e POSTGRES_USER={POSTGRES_USER} "
            f"-e POSTGRES_PASSWORD={POSTGRES_PASSWORD} "
            f"-e POSTGRES_DB={POSTGRES_DB} "
            f"-p {DEFAULT_PORT}:5432 {DEFAULT_POSTGRES_IMAGE}"
        )

        print("Waiting for database to be ready...")
        time.sleep(5)

        print("Applying existing Alembic migrations...")
        run("alembic upgrade head")

        if check:
            print("Running 'alembic check'...")
            run("alembic check")
        elif revision_message:
            print("Generating new Alembic revision...")
            if autogenerate:
                run(f'alembic revision --autogenerate -m "{revision_message}"')
            else:
                run(f'alembic revision -m "{revision_message}"')

        if print_schema:
            print_db_schema()

    finally:
        print("Cleaning up: stopping container...")
        subprocess.run(f"docker stop {DB_CONTAINER_NAME}", shell=True)


if __name__ == "__main__":
    generate_migrations()

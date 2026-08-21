# AGENTS.md

## Setup

* UV for package management
* Run: `uv run <command>`
* Test: `uv run pytest`
* CI checks: `uv run ruff check app tests`, `uv run black app tests`, `uv run mypy app --ignore-missing-imports`
* Settings in `app/config.py`: set by .env or env variables

## Project data

This app only manages workflows. Source of truth for workflows/results is generally:

* Seqera
* S3
* Globus

Need to consider how data will be synced when making changes.

## Code style

* FastAPI: reusable dependencies preferred
* SQLAlchemy: changes to models should be methods on the model
* Pydantic schemas for input and responses
* Suggest established libraries instead of creating our own parsers, clients, etc.
* Create client classes for APIs (AsyncSeqeraClient)
* Avoid creating one-off private functions: shared, reusable code preferred

Guidelines for simple code + less duplication:

1. Does this need to exist?   → no: skip it
2. Already in this codebase?  → reuse it, don't rewrite
3. Stdlib does it?            → use it
4. Native platform feature?   → use it
5. Installed dependency?      → use it
6. One line?                  → one line
7. Only then: the minimum that works

## Tests

* Use factories to create database objects
* Prefer actual/mocked objects over `SimpleNamespace`
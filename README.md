# SBP Portal Backend Server

![Lint](https://github.com/AustralianBioCommons/sbp-backend/actions/workflows/lint.yml/badge.svg)
![Coverage](https://github.com/AustralianBioCommons/sbp-backend/actions/workflows/test-coverage.yml/badge.svg)
[![codecov](https://codecov.io/gh/AustralianBioCommons/sbp-backend/branch/main/graph/badge.svg)](https://codecov.io/gh/AustralianBioCommons/sbp-backend)

FastAPI backend for handling Seqera Platform workflow launches.

## Prerequisites

- Python 3.10+
- [UV](https://docs.astral.sh/uv/) package manager

## Setup

1. Install UV (if not already installed):

   ```bash
   # macOS/Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   
   # Windows
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

2. Install dependencies:

   ```bash
   uv sync --all-extras
   ```

3. Configure environment variables:

   ```bash
   cp .env.example .env
   # Edit .env with your Seqera Platform credentials
   ```

4. Run the API locally:

   ```bash
   uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 3000
   ```

## API Endpoints

- `GET /health` — Lightweight health probe
- `POST /api/workflows/launch` — Launch a Seqera workflow (send the same payload previously expected by the Express server)
- `POST /api/workflows/{runId}/cancel` — Placeholder response for cancelling a run
- `GET /api/workflows/runs` — Placeholder list endpoint mirroring the previous Node API
- `GET /api/workflows/{runId}/logs` — Placeholder log endpoint
- `GET /api/workflows/{runId}/details` — Placeholder details endpoint
- `POST /api/workflows/datasets/upload` — Create a Seqera dataset and upload submitted form data as a CSV

## Database Schema

The database schema is managed by SQLAlchemy and Alembic migrations. To visualize the current schema:

```bash
# Generate an up-to-date schema diagram from SQLAlchemy models
uv run python generate_schema_diagram.py
```

This creates [docs/schema_diagram.png](docs/schema_diagram.png) showing all tables, relationships, and constraints. The diagram is always generated from the actual SQLAlchemy models, ensuring it stays in sync with your database structure.

**Note:** The diagram requires [Graphviz](https://graphviz.org/) to be installed on your system:
- macOS: `brew install graphviz`
- Ubuntu/Debian: `apt-get install graphviz`
- Windows: Download from [graphviz.org](https://graphviz.org/download/)

## Testing

Run the test suite with coverage:

```bash
# Run all tests with coverage report
uv run pytest --cov=app --cov-report=term-missing --cov-report=html

# Run tests with verbose output
uv run pytest -v

# Run specific test file
uv run pytest tests/test_main.py

# Check coverage threshold (90%)
uv run coverage report --fail-under=90
```

View HTML coverage report:

```bash
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
start htmlcov/index.html  # Windows (Command Prompt / PowerShell)
```

## Linting and Code Quality

```bash
# Run ruff linter
uv run ruff check app tests

# Run black formatter
uv run black app tests

# Run type checking with mypy
uv run mypy app --ignore-missing-imports

# Install pre-commit hooks
uv run pre-commit install

# Run pre-commit on all files
pre-commit run --all-files
```

## Environment Variables

Required entries in `.env`:

- `SEQERA_API_URL` — Seqera Platform API endpoint (e.g., `https://api.seqera.io`)
- `SEQERA_ACCESS_TOKEN` — API access token
- `COMPUTE_ID` — Default compute environment ID
- `WORK_DIR` — Default work directory
- `WORK_SPACE` — Seqera workspace identifier
- `ALLOWED_ORIGINS` — (Optional) comma-separated list of origins allowed via CORS (defaults to `https://dev.sbp.test.biocommons.org.au`)
- `PORT` — (Optional) uvicorn port when running `python -m app.main`
- `UVICORN_RELOAD` — (Optional) set to `true` to enable reload when running via `python -m app.main`

## Containerization

1. Build the image from the repository root:

   ```bash
   docker build -t sbp-backend .
   ```

2. Run the container, passing your `.env` file (or explicit `-e` overrides) so the API can reach Seqera:

   ```bash
   docker run --rm -p 3000:3000 --env-file .env sbp-backend
   ```

   Override `PORT` or uvicorn flags in the env file if you need different bindings. Any value defined in `.env` becomes available to the app inside the container.

## Notes

- Requests fail fast with `500` if mandatory environment variables are missing.
- Downstream Seqera API failures surface as a `502` response with the original error message for easier debugging.

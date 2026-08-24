# SBP Portal Backend Server

![Lint](https://github.com/AustralianBioCommons/sbp-backend/actions/workflows/lint.yml/badge.svg)
![Coverage](https://github.com/AustralianBioCommons/sbp-backend/actions/workflows/test-coverage.yml/badge.svg)
[![codecov](https://codecov.io/gh/AustralianBioCommons/sbp-backend/branch/main/graph/badge.svg)](https://codecov.io/gh/AustralianBioCommons/sbp-backend)

FastAPI backend for handling Seqera Platform workflow launches.

## Prerequisites

- Python 3.14
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

5. Run the job scheduler locally (you probably want `--dry-run` so jobs aren't submitted to Seqera)

   ```bash
   uv run python app/run_scheduler.py --dry-run
   ```

## API Endpoints

- `GET /health` — Lightweight health probe
- `GET /api/health/components` — Coarse, user-facing component health for the portal banner (requires `Authorization: Bearer <access_token>`)
- `GET /api/health/agent` — Machine-only Tower Agent health for automated monitoring (requires an M2M token with the `read:agent-health` permission)
- `POST /api/workflows/launch` — Launch a Seqera workflow (requires `Authorization: Bearer <access_token>`)
- `GET /api/jobs` — List jobs for the authenticated user (requires `Authorization: Bearer <access_token>`)
- `GET /api/jobs/{run_id}` — Get one job for the authenticated user (requires `Authorization: Bearer <access_token>`)
- `POST /api/jobs/{run_id}/cancel` — Cancel a workflow run (requires `Authorization: Bearer <access_token>`)
- `DELETE /api/jobs/{run_id}` — Delete one job for the authenticated user (requires `Authorization: Bearer <access_token>`)
- `POST /api/jobs/bulk-delete` — Delete multiple jobs for the authenticated user (requires `Authorization: Bearer <access_token>`)
- `GET /api/results/{run_id}/settingParams` — Get submitted settings for one result view (requires `Authorization: Bearer <access_token>`)
- `GET /api/results/{run_id}/logs` — Get Seqera logs for one result view (requires `Authorization: Bearer <access_token>`)
- `GET /api/results/{run_id}/report` — Get the primary HTML animation/report link for portal display (requires `Authorization: Bearer <access_token>`)
- `GET /api/results/{run_id}/downloads` — Get non-snapshot result download links such as reports, CSV outputs, and PDB files (requires `Authorization: Bearer <access_token>`)
- `GET /api/results/{run_id}/snapshots` — Get snapshot image download links only (requires `Authorization: Bearer <access_token>`)
- `POST /api/workflows/datasets/upload` — Create a Seqera dataset and upload submitted form data as a CSV
- `POST /api/workflows/pdb/upload` — Upload a PDB file
- `GET /files` — List S3 files
- `GET /csv/{file_key}` — Read CSV rows from S3
- `GET /run/{run_id}/max-score` — Fetch max score for a run
- `GET /admin` — Optional Starlette Admin UI for database debugging (disabled by default)

## Database Schema

The database schema is managed by SQLAlchemy and Alembic migrations. To visualize the current schema:

```bash
# Generate an up-to-date schema diagram from SQLAlchemy models
bash generate_db_diagram.sh
```

This creates [docs/schema_diagram.svg](docs/schema_diagram.svg) showing all tables, relationships, and constraints. The diagram is always generated from the actual SQLAlchemy models, ensuring it stays in sync with your database structure.

### Updating the Database Schema Diagram

When database models are changed (added, removed, or modified), the database schema diagram should be updated to reflect the changes. Run the following command:

```bash
bash generate_db_diagram.sh
```

The updated diagram will be saved in `docs/schema_diagram.svg`. Make sure to commit this file along with your model changes.

**Note:** The diagram requires [Graphviz](https://graphviz.org/) to be installed on your system:
- macOS: `brew install graphviz`
- Ubuntu/Debian: `apt-get install graphviz`
- Windows: Download from [graphviz.org](https://graphviz.org/download/)

## Database Migrations

Migrations are managed with Alembic and must be committed manually alongside model changes. The `generate_migrations.py` script spins up a temporary Postgres container, applies existing migrations, and then either generates a new revision or checks that the schema is in sync.

**Prerequisites:** Docker must be running.

```bash
# Generate a new migration from model changes (most common)
uv run python generate_migrations.py -m "describe your change here"

# Check that migrations are in sync with the current models (no new revision needed)
uv run python generate_migrations.py --check

# Create a blank migration (fill in upgrade/downgrade manually)
uv run python generate_migrations.py -m "describe your change here" --no-autogenerate

# Print the live DB schema after applying migrations
uv run python generate_migrations.py --check --print-schema
```

After generating, commit the new file from `alembic/versions/` alongside your model changes.

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

- `DATABASE_URL` — SQLAlchemy database URL.
- `PORT` — Uvicorn port when running `python -m app.main`.
- `ALLOWED_ORIGINS` — Comma-separated list of origins allowed via CORS.
- `SEQERA_API_URL` — Seqera Platform API endpoint (e.g., `https://api.seqera.io`).
- `SEQERA_ACCESS_TOKEN` — Seqera API access token.
- `SEQERA_COMPUTE_ID` — Default Seqera compute environment ID.
- `SEQERA_WORK_DIR` — Default Seqera work directory.
- `SEQERA_WORK_SPACE` — Seqera workspace identifier.
- `AWS_ACCESS_KEY_ID` — AWS access key for S3 access.
- `AWS_SECRET_ACCESS_KEY` — AWS secret key for S3 access.
- `AWS_REGION` — AWS region.
- `AWS_S3_BUCKET` — Private bucket used for workflow inputs and outputs.
- `AUTH_DOMAIN` — Auth tenant domain used for JWKS lookup and token issuer validation.
- `AUTH_CLIENT_ID` — OAuth client ID.
- `AUTH_AUDIENCE` — Expected audience claim in incoming bearer tokens.
- `AUTH_REDIRECT_URI` — Auth callback URI.
- `AUTH_REQUIRED_ROLE` — Admin role required for Starlette Admin access.
- `AUTH_WORKFLOW_EXECUTION_ROLE` — Role required to launch workflows.
- `DB_ADMIN_AUTH_REDIRECT_URI` — Admin auth callback URI.
- `DB_ADMIN_COOKIE_SECURE` — Whether admin cookies are marked secure.
- `DB_ADMIN_FORBIDDEN_HOME_URL` — URL shown/used when admin access is denied.
- `DB_ADMIN_ROLES_CLAIM` — Token claim that contains role strings.
- `DB_ADMIN_SESSION_SECRET` — Secret used to sign admin sessions. Set a strong random value.

Optional entries:

- `UVICORN_RELOAD` — Set to `true` to enable reload when running via `python -m app.main` (default `false`).
- `ENABLE_DB_ADMIN` — Set to `true` to enable Starlette Admin at `/admin` (default `false`).
- `ENABLE_CREDITS` — Set to `true` to enforce workflow credit checks (default `false`).
- `DB_ADMIN_TITLE` — Admin UI title (default `SBP Backend Admin`). This is still read directly by the admin package setup.
- `AUTH_CLIENT_SECRET` — OAuth client secret for admin login, if the auth provider requires it.
- `AUTH_ISSUER` — Custom issuer URL to accept in addition to `https://{AUTH_DOMAIN}/`.
- `AUTH_ALGORITHMS` — Comma-separated JWT algorithms (default `RS256`).
- `SEQERA_GADI_PROJECT` — NCI Gadi project code used for PBS submissions (default `yz52`).
- `SEQERA_MAX_CONCURRENT_WORKFLOWS` — Scheduler submission cap based on active Seqera workflows (default `25`).
- `SEQERA_WORKFLOW_SYNC_BATCH_LIMIT` — Workflow sync batch size for the scheduler (default `50`).
- `SEQERA_HEALTH_CACHE_TTL_SECONDS` — Cache TTL for system status probes in seconds (default `30`).
- `SEQERA_ENABLE_AGENT_HEALTHCHECK` — Set to `true` to enable the active Tower Agent liveness probe (default `false`).
- `SEQERA_HEALTHCHECK_AGENT_TIMEOUT_SECONDS` — Max seconds to wait for throwaway env validation (default `20`).
- `AWS_LOG_GROUP` — Backend CloudWatch log group name for the admin System Status link.

## DB Debug UI (Starlette Admin)

Enable local DB debugging UI:

```bash
export AUTH_DOMAIN="your-auth-domain.example.com"
export AUTH_CLIENT_ID="your-auth-client-id"
export AUTH_AUDIENCE="https://your-auth-audience.example.com"
export AUTH_REDIRECT_URI="http://localhost:3000/auth/callback"
export AUTH_REQUIRED_ROLE="biocommons/role/sbp/admin"
export DB_ADMIN_AUTH_REDIRECT_URI="http://localhost:3000/admin/login"
export DB_ADMIN_COOKIE_SECURE="false"
export DB_ADMIN_FORBIDDEN_HOME_URL="http://localhost:3000/"
export DB_ADMIN_ROLES_CLAIM="https://biocommons.org.au/roles"
export DB_ADMIN_SESSION_SECRET="replace-with-long-random-secret"
ENABLE_DB_ADMIN=true uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 3000
```

Then open `http://localhost:3000/admin`.

Use this only in trusted/internal environments.

## System Status (admin only)

Two admin-only probes report the runtime health of the components workflow
submission depends on:

- **Seqera API reachability + credentials** — authenticated `GET {SEQERA_API_URL}/user-info`.
  A 2xx confirms the platform is reachable *and* `SEQERA_ACCESS_TOKEN` is valid;
  401/403 is reported as a credential problem.
- **Compute environment status** — reads `GET /compute-envs/{SEQERA_COMPUTE_ID}?workspaceId={SEQERA_WORK_SPACE}`
  and maps the `computeEnv.status` field (the Seqera Tower agent connection state,
  our proxy for Gadi-side health): `AVAILABLE` → healthy, `CREATING` → degraded,
  `ERRORED`/`OFFLINE`/`INVALID` → unhealthy, anything else → degraded. This call is
  workspace-scoped, so it also validates `SEQERA_WORK_SPACE`/`SEQERA_COMPUTE_ID`
  access (a 403/404 is surfaced as a config hint).
- **Tower Agent liveness** (opt-in via `SEQERA_ENABLE_AGENT_HEALTHCHECK=true`) — actively
  verifies the agent: clones `SEQERA_COMPUTE_ID` (reusing its platform/config/tw-agent
  credential) into a throwaway env named `sbp-agent-healthcheck-*`, which forces
  Seqera to validate the agent connection, reads the resulting status
  (`AVAILABLE` → healthy, `ERRORED`/`INVALID` → unhealthy, still `CREATING` after
  `SEQERA_HEALTHCHECK_AGENT_TIMEOUT_SECONDS` → degraded), then deletes the throwaway env.
  ⚠️ This **mutates Seqera state** (creates + deletes a compute env on every probe,
  i.e. roughly once per cache TTL), which is why it is off by default.

Surfaces:

- `GET /admin/api/system-status` — admin-only JSON with per-component status,
  latency, last-error body, and the full Seqera compute-env JSON. Pass
  `?refresh=true` to bypass the short-lived cache. Results are cached for
  `SEQERA_HEALTH_CACHE_TTL_SECONDS` (default 30s) with stampede protection. This endpoint
  is always mounted (independent of `ENABLE_DB_ADMIN`) and only requires an admin
  token, so it is also suitable for healthchecks / external monitoring.
- `/admin/system-status` — the **System Status** dashboard view (requires
  `ENABLE_DB_ADMIN=true`; auto-refreshes every 30s) rendering a per-component grid
  with status pills and an optional one-click link to the backend CloudWatch log
  group.
- `GET /api/health/agent` — machine-only Tower Agent health for automated
  monitoring (e.g. a restart script). Authenticated by an Auth0 M2M token with
  the `read:agent-health` permission rather than a human workflow role; no
  `app_users` row is created. Returns the cached agent `status`, `checkedAt`, and
  a short `message` (no raw probe detail). When the Tower Agent component is
  absent (agent monitoring disabled) it returns `503` rather than a fabricated
  `unhealthy`, so callers treat it as a monitoring/config failure, not a restart
  signal.

Relevant environment variables:

- `SEQERA_HEALTH_CACHE_TTL_SECONDS` — (Optional) probe cache TTL in seconds (default `30`).
- `SEQERA_ENABLE_AGENT_HEALTHCHECK` — (Optional) set `true` to enable the active Tower Agent
  clone-create-delete liveness probe (default `false`; mutates Seqera state).
- `SEQERA_HEALTHCHECK_AGENT_TIMEOUT_SECONDS` — (Optional) max wait for the throwaway env to
  validate before reporting degraded (default `20`).
- `AWS_LOG_GROUP` — (Optional) backend CloudWatch log group name; when set,
  the dashboard shows a one-click link to it. Uses `AWS_REGION` for the console URL.

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
   The container enables uvicorn proxy headers so deployments behind ALB preserve HTTPS URL generation for admin assets and redirects.

## Notes

- Requests fail fast with `500` if mandatory environment variables are missing.
- Downstream Seqera API failures surface as a `502` response with the original error message for easier debugging.

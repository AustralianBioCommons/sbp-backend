Design: Seqera Workflow Run Provenance Schema (PostgreSQL 17)

Goal
Design a relational model that tracks end-to-end workflow execution, links all inputs and outputs to S3, and preserves clear ownership by application users while using a single Seqera service account for execution.

Logical Data Model
Entities
- app_users: application users who own workflows and runs
- workflows: registered workflow definitions (optional but useful for grouping)
- seqera_accounts: service account identities used to execute runs (single row expected)
- workflow_runs: execution instances, owned by app_users, executed via seqera_accounts
- run_inputs: typed inputs for a workflow run, referencing S3 objects
- run_outputs: typed outputs for a workflow run, referencing S3 objects
- s3_objects: canonical S3 object references used by inputs/outputs
- run_status_events: status transitions and timestamps for auditing/provenance
- run_metrics: selected metrics (e.g., avg score) and flexible JSON for extras

Relationships
- app_users 1--* workflow_runs
- workflows 1--* workflow_runs
- seqera_accounts 1--* workflow_runs
- workflow_runs 1--* run_inputs
- workflow_runs 1--* run_outputs
- s3_objects 1--* run_inputs
- s3_objects 1--* run_outputs
- workflow_runs 1--* run_status_events
- workflow_runs 1--1 run_metrics (optional)

Relational Schema (DDL Skeleton)
Note: This is a design-level schema definition; use timestamptz and UUIDs for portability.

CREATE TABLE app_users (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  email text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workflows (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  description text,
  repo_url text,
  default_revision text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE seqera_accounts (
  id uuid PRIMARY KEY,
  label text NOT NULL,
  workspace_id text NOT NULL,
  service_account_email text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workflow_runs (
  id uuid PRIMARY KEY,
  workflow_id uuid REFERENCES workflows(id) ON DELETE SET NULL,
  owner_user_id uuid NOT NULL REFERENCES app_users(id) ON DELETE RESTRICT,
  seqera_account_id uuid NOT NULL REFERENCES seqera_accounts(id) ON DELETE RESTRICT,
  seqera_dataset_id text,
  seqera_run_id text NOT NULL,
  run_name text,
  status text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  parameters jsonb,
  labels jsonb,
  error_summary text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (seqera_run_id)
);

CREATE TABLE s3_objects (
  id uuid PRIMARY KEY,
  bucket text NOT NULL,
  object_key text NOT NULL,
  version_id text,
  etag text,
  size_bytes bigint,
  checksum_sha256 text,
  content_type text,
  region text,
  storage_class text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bucket, object_key, version_id)
);

CREATE TABLE run_inputs (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  s3_object_id uuid NOT NULL REFERENCES s3_objects(id) ON DELETE RESTRICT,
  input_type text NOT NULL,
  label text,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE run_outputs (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  s3_object_id uuid NOT NULL REFERENCES s3_objects(id) ON DELETE RESTRICT,
  output_type text NOT NULL,
  label text,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE run_status_events (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
  status text NOT NULL,
  note text,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb
);

CREATE TABLE run_metrics (
  run_id uuid PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
  average_score numeric,
  metrics jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

Indexing Strategy (UI Queries)
Run list (filter by user, status, creation date):
- CREATE INDEX workflow_runs_owner_created_idx ON workflow_runs (owner_user_id, created_at DESC);
- CREATE INDEX workflow_runs_status_created_idx ON workflow_runs (status, created_at DESC);
- CREATE INDEX workflow_runs_created_idx ON workflow_runs (created_at DESC);

Run details (by run id, including seqera id):
- CREATE UNIQUE INDEX workflow_runs_seqera_run_idx ON workflow_runs (seqera_run_id);

Execution artifacts (inputs/outputs by run, by type):
- CREATE INDEX run_inputs_run_type_idx ON run_inputs (run_id, input_type);
- CREATE INDEX run_outputs_run_type_idx ON run_outputs (run_id, output_type);

S3 lookups (object traceability):
- CREATE INDEX s3_objects_bucket_key_idx ON s3_objects (bucket, object_key);

Design Rationale
User vs service account separation
- workflow_runs.owner_user_id captures application ownership for access control and audit.
- workflow_runs.seqera_account_id captures the execution mechanism separately.
- No user identity is derived from Seqera; rotation of Seqera credentials does not affect ownership.

Provenance and traceability
- S3 objects are normalized in s3_objects for immutable references (bucket/key/version).
- Inputs and outputs reference s3_objects with typed fields (input_type/output_type) plus metadata JSONB.
- run_status_events records the full lifecycle with timestamps for audit/debugging.
- run_metrics stores known metrics (average_score) and a JSONB blob for additional metrics without migrations.

Assumptions
- A single Seqera account is used in production, but the schema supports multiple.
- status is a controlled string enum in application code (e.g., queued, running, succeeded, failed, canceled).
- parameters and labels are flexible and stored as JSONB.

Reference Design Notes for AAI
- Ownership and access decisions are based on app_users and workflow_runs.owner_user_id.
- External service identity (Seqera) is captured only as a service account to avoid identity conflation.

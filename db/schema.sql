-- DDL for core schema (aligned with Alembic migration).

CREATE TABLE app_users (
    id UUID NOT NULL,
    auth0_user_id UUID NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    CONSTRAINT app_users_pkey PRIMARY KEY (id),
    CONSTRAINT app_users_auth0_user_id_unique UNIQUE (auth0_user_id),
    CONSTRAINT app_users_email_unique UNIQUE (email)
);

CREATE TABLE workflows (
    id UUID NOT NULL,
    name TEXT NOT NULL,
    description TEXT NULL,
    repo_url TEXT NULL,
    default_revision TEXT NULL,
    CONSTRAINT workflows_pkey PRIMARY KEY (id)
);

CREATE TABLE workflow_runs (
    id UUID NOT NULL,
    workflow_id UUID NULL,
    owner_user_id UUID NOT NULL,
    seqera_dataset_id TEXT NULL,
    seqera_run_id TEXT NOT NULL,
    run_name TEXT NULL,
    work_dir BIGINT NOT NULL,
    CONSTRAINT workflow_runs_pkey PRIMARY KEY (id),
    CONSTRAINT workflow_runs_owner_user_id_foreign FOREIGN KEY (owner_user_id)
        REFERENCES app_users (id),
    CONSTRAINT workflow_runs_workflow_id_foreign FOREIGN KEY (workflow_id)
        REFERENCES workflows (id),
    CONSTRAINT workflow_runs_seqera_run_id_unique UNIQUE (seqera_run_id),
    CONSTRAINT workflow_runs_work_dir_unique UNIQUE (work_dir)
);

CREATE TABLE s3_objects (
    "URI" TEXT NOT NULL,
    object_key TEXT NOT NULL,
    version_id TEXT NULL,
    size_bytes BIGINT NULL,
    CONSTRAINT s3_objects_pkey PRIMARY KEY (object_key),
    CONSTRAINT s3_objects_uri_unique UNIQUE ("URI")
);

CREATE TABLE run_metrics (
    run_id UUID NOT NULL,
    max_score DECIMAL(8, 2) NULL,
    CONSTRAINT run_metrics_pkey PRIMARY KEY (run_id),
    CONSTRAINT run_metrics_run_id_foreign FOREIGN KEY (run_id)
        REFERENCES workflow_runs (id)
);

CREATE TABLE run_inputs (
    run_id UUID NOT NULL,
    s3_object_id TEXT NOT NULL,
    CONSTRAINT run_inputs_pkey PRIMARY KEY (run_id, s3_object_id),
    CONSTRAINT run_inputs_run_id_foreign FOREIGN KEY (run_id)
        REFERENCES workflow_runs (id),
    CONSTRAINT run_inputs_s3_object_id_foreign FOREIGN KEY (s3_object_id)
        REFERENCES s3_objects (object_key)
);

CREATE TABLE run_outputs (
    run_id UUID NOT NULL,
    s3_object_id TEXT NOT NULL,
    CONSTRAINT run_outputs_pkey PRIMARY KEY (run_id, s3_object_id),
    CONSTRAINT run_outputs_run_id_foreign FOREIGN KEY (run_id)
        REFERENCES workflow_runs (id),
    CONSTRAINT run_outputs_s3_object_id_foreign FOREIGN KEY (s3_object_id)
        REFERENCES s3_objects (object_key)
);

-- Index definitions (unique constraints create indexes automatically in Postgres).

CREATE UNIQUE INDEX app_users_auth0_user_id_unique
    ON app_users (auth0_user_id);

CREATE UNIQUE INDEX app_users_email_unique
    ON app_users (email);

CREATE UNIQUE INDEX workflow_runs_seqera_run_id_unique
    ON workflow_runs (seqera_run_id);

CREATE UNIQUE INDEX workflow_runs_work_dir_unique
    ON workflow_runs (work_dir);

CREATE UNIQUE INDEX s3_objects_uri_unique
    ON s3_objects ("URI");

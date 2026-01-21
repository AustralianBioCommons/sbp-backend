-- Constraint definitions (for reference).

ALTER TABLE app_users
    ADD CONSTRAINT app_users_pkey PRIMARY KEY (id),
    ADD CONSTRAINT app_users_auth0_user_id_unique UNIQUE (auth0_user_id),
    ADD CONSTRAINT app_users_email_unique UNIQUE (email);

ALTER TABLE workflows
    ADD CONSTRAINT workflows_pkey PRIMARY KEY (id);

ALTER TABLE workflow_runs
    ADD CONSTRAINT workflow_runs_pkey PRIMARY KEY (id),
    ADD CONSTRAINT workflow_runs_owner_user_id_foreign FOREIGN KEY (owner_user_id)
        REFERENCES app_users (id),
    ADD CONSTRAINT workflow_runs_workflow_id_foreign FOREIGN KEY (workflow_id)
        REFERENCES workflows (id),
    ADD CONSTRAINT workflow_runs_seqera_run_id_unique UNIQUE (seqera_run_id),
    ADD CONSTRAINT workflow_runs_work_dir_unique UNIQUE (work_dir);

ALTER TABLE s3_objects
    ADD CONSTRAINT s3_objects_pkey PRIMARY KEY (object_key),
    ADD CONSTRAINT s3_objects_uri_unique UNIQUE ("URI");

ALTER TABLE run_metrics
    ADD CONSTRAINT run_metrics_pkey PRIMARY KEY (run_id),
    ADD CONSTRAINT run_metrics_run_id_foreign FOREIGN KEY (run_id)
        REFERENCES workflow_runs (id);

ALTER TABLE run_inputs
    ADD CONSTRAINT run_inputs_pkey PRIMARY KEY (run_id, s3_object_id),
    ADD CONSTRAINT run_inputs_run_id_foreign FOREIGN KEY (run_id)
        REFERENCES workflow_runs (id),
    ADD CONSTRAINT run_inputs_s3_object_id_foreign FOREIGN KEY (s3_object_id)
        REFERENCES s3_objects (object_key);

ALTER TABLE run_outputs
    ADD CONSTRAINT run_outputs_pkey PRIMARY KEY (run_id, s3_object_id),
    ADD CONSTRAINT run_outputs_run_id_foreign FOREIGN KEY (run_id)
        REFERENCES workflow_runs (id),
    ADD CONSTRAINT run_outputs_s3_object_id_foreign FOREIGN KEY (s3_object_id)
        REFERENCES s3_objects (object_key);

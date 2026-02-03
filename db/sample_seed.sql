-- Sample seed data for local testing of workflow ownership filters.
-- Safe to run multiple times (uses ON CONFLICT upserts where possible).

BEGIN;

-- Users used by the X-Auth0-User-Id header in API calls
INSERT INTO app_users (id, auth0_user_id, name, email)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'auth0|alice-dev', 'Alice Dev', 'alice@example.com'),
    ('22222222-2222-2222-2222-222222222222', 'auth0|bob-dev', 'Bob Dev', 'bob@example.com')
ON CONFLICT (auth0_user_id) DO UPDATE
SET
    name = EXCLUDED.name,
    email = EXCLUDED.email;

INSERT INTO workflows (id, name, description, repo_url, default_revision)
VALUES
    (
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        'Bindflow',
        'Sample Bindflow workflow for local ownership tests',
        'https://github.com/Australian-Structural-Biology-Computing/bindflow',
        'dev'
    )
ON CONFLICT (id) DO NOTHING;

-- Runs owned by Alice
INSERT INTO workflow_runs (
    id, workflow_id, owner_user_id, seqera_dataset_id, seqera_run_id, run_name, work_dir
)
VALUES
    (
        'aaaa1111-aaaa-1111-aaaa-111111111111',
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        '11111111-1111-1111-1111-111111111111',
        'dataset-alice-1',
        '292kB09WsB4RyC',
        'anne_test_2',
        1001
    ),
    (
        'aaaa2222-aaaa-2222-aaaa-222222222222',
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        '11111111-1111-1111-1111-111111111111',
        'dataset-alice-2',
        'B5U7ok7AAyCpV',
        'naughty_goldstine',
        1002
    )
ON CONFLICT (seqera_run_id) DO NOTHING;

-- Runs owned by Bob
INSERT INTO workflow_runs (
    id, workflow_id, owner_user_id, seqera_dataset_id, seqera_run_id, run_name, work_dir
)
VALUES
    (
        'bbbb1111-bbbb-1111-bbbb-111111111111',
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        '22222222-2222-2222-2222-222222222222',
        'dataset-bob-1',
        '30Nta1wQivKcS',
        'marvelous_lamarck',
        2001
    )
ON CONFLICT (seqera_run_id) DO NOTHING;

-- Optional metrics for score field
INSERT INTO run_metrics (run_id, max_score)
VALUES
    ('aaaa1111-aaaa-1111-aaaa-111111111111', 0.93),
    ('aaaa2222-aaaa-2222-aaaa-222222222222', 0.71),
    ('bbbb1111-bbbb-1111-bbbb-111111111111', 0.62)
ON CONFLICT (run_id) DO UPDATE
SET max_score = EXCLUDED.max_score;

COMMIT;

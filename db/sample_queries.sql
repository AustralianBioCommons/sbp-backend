-- Sample queries

-- Listing workflow runs
SELECT
    wr.id,
    wr.seqera_run_id,
    wr.run_name,
    wr.seqera_dataset_id,
    wr.work_dir,
    wr.workflow_id,
    w.name AS workflow_name,
    wr.owner_user_id,
    u.email AS owner_email
FROM workflow_runs AS wr
LEFT JOIN workflows AS w ON w.id = wr.workflow_id
JOIN app_users AS u ON u.id = wr.owner_user_id
ORDER BY wr.seqera_run_id;

-- Fetching run details (by run id)
SELECT
    wr.id,
    wr.seqera_run_id,
    wr.run_name,
    wr.seqera_dataset_id,
    wr.work_dir,
    wr.workflow_id,
    w.name AS workflow_name,
    w.description AS workflow_description,
    wr.owner_user_id,
    u.name AS owner_name,
    u.email AS owner_email,
    rm.max_score
FROM workflow_runs AS wr
LEFT JOIN workflows AS w ON w.id = wr.workflow_id
JOIN app_users AS u ON u.id = wr.owner_user_id
LEFT JOIN run_metrics AS rm ON rm.run_id = wr.id
WHERE wr.id = $1;

-- Resolving inputs and outputs (by run id)
SELECT
    'input' AS io_type,
    s.object_key,
    s."URI" AS uri,
    s.version_id,
    s.size_bytes
FROM run_inputs AS ri
JOIN s3_objects AS s ON s.object_key = ri.s3_object_id
WHERE ri.run_id = $1

UNION ALL

SELECT
    'output' AS io_type,
    s.object_key,
    s."URI" AS uri,
    s.version_id,
    s.size_bytes
FROM run_outputs AS ro
JOIN s3_objects AS s ON s.object_key = ro.s3_object_id
WHERE ro.run_id = $1
ORDER BY io_type, object_key;

"""rename_constraints_to_match_naming_convention

Renames all constraints to follow the standard naming convention:
- uq_<table_name>_<column_name> for unique constraints
- fk_<table_name>_<column_name>_<referred_table_name> for foreign keys
- pk_<table_name> for primary keys
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a0a3098c22f'
down_revision = '20260119_000001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename unique constraints on app_users
    op.drop_constraint('app_users_auth0_user_id_unique', 'app_users', type_='unique')
    op.create_unique_constraint('uq_app_users_auth0_user_id', 'app_users', ['auth0_user_id'])
    
    op.drop_constraint('app_users_email_unique', 'app_users', type_='unique')
    op.create_unique_constraint('uq_app_users_email', 'app_users', ['email'])
    
    # Rename unique constraints on workflow_runs
    op.drop_constraint('workflow_runs_seqera_run_id_unique', 'workflow_runs', type_='unique')
    op.create_unique_constraint('uq_workflow_runs_seqera_run_id', 'workflow_runs', ['seqera_run_id'])
    
    op.drop_constraint('workflow_runs_work_dir_unique', 'workflow_runs', type_='unique')
    op.create_unique_constraint('uq_workflow_runs_work_dir', 'workflow_runs', ['work_dir'])
    
    # Rename foreign keys on workflow_runs
    op.drop_constraint('workflow_runs_owner_user_id_foreign', 'workflow_runs', type_='foreignkey')
    op.create_foreign_key('fk_workflow_runs_owner_user_id_app_users', 'workflow_runs', 'app_users', ['owner_user_id'], ['id'])
    
    op.drop_constraint('workflow_runs_workflow_id_foreign', 'workflow_runs', type_='foreignkey')
    op.create_foreign_key('fk_workflow_runs_workflow_id_workflows', 'workflow_runs', 'workflows', ['workflow_id'], ['id'])
    
    # Rename unique constraint on s3_objects
    op.drop_constraint('s3_objects_uri_unique', 's3_objects', type_='unique')
    op.create_unique_constraint('uq_s3_objects_URI', 's3_objects', ['URI'])
    
    # Rename foreign key on run_metrics
    op.drop_constraint('run_metrics_run_id_foreign', 'run_metrics', type_='foreignkey')
    op.create_foreign_key('fk_run_metrics_run_id_workflow_runs', 'run_metrics', 'workflow_runs', ['run_id'], ['id'])
    
    # Rename foreign keys on run_inputs
    op.drop_constraint('run_inputs_run_id_foreign', 'run_inputs', type_='foreignkey')
    op.create_foreign_key('fk_run_inputs_run_id_workflow_runs', 'run_inputs', 'workflow_runs', ['run_id'], ['id'])
    
    op.drop_constraint('run_inputs_s3_object_id_foreign', 'run_inputs', type_='foreignkey')
    op.create_foreign_key('fk_run_inputs_s3_object_id_s3_objects', 'run_inputs', 's3_objects', ['s3_object_id'], ['object_key'])
    
    # Rename primary key on run_inputs
    op.drop_constraint('run_inputs_pkey', 'run_inputs', type_='primary')
    op.create_primary_key('pk_run_inputs', 'run_inputs', ['run_id', 's3_object_id'])
    
    # Rename foreign keys on run_outputs
    op.drop_constraint('run_outputs_run_id_foreign', 'run_outputs', type_='foreignkey')
    op.create_foreign_key('fk_run_outputs_run_id_workflow_runs', 'run_outputs', 'workflow_runs', ['run_id'], ['id'])
    
    op.drop_constraint('run_outputs_s3_object_id_foreign', 'run_outputs', type_='foreignkey')
    op.create_foreign_key('fk_run_outputs_s3_object_id_s3_objects', 'run_outputs', 's3_objects', ['s3_object_id'], ['object_key'])
    
    # Rename primary key on run_outputs
    op.drop_constraint('run_outputs_pkey', 'run_outputs', type_='primary')
    op.create_primary_key('pk_run_outputs', 'run_outputs', ['run_id', 's3_object_id'])


def downgrade() -> None:
    # Revert run_outputs
    op.drop_constraint('pk_run_outputs', 'run_outputs', type_='primary')
    op.create_primary_key('run_outputs_pkey', 'run_outputs', ['run_id', 's3_object_id'])
    
    op.drop_constraint('fk_run_outputs_s3_object_id_s3_objects', 'run_outputs', type_='foreignkey')
    op.create_foreign_key('run_outputs_s3_object_id_foreign', 'run_outputs', 's3_objects', ['s3_object_id'], ['object_key'])
    
    op.drop_constraint('fk_run_outputs_run_id_workflow_runs', 'run_outputs', type_='foreignkey')
    op.create_foreign_key('run_outputs_run_id_foreign', 'run_outputs', 'workflow_runs', ['run_id'], ['id'])
    
    # Revert run_inputs
    op.drop_constraint('pk_run_inputs', 'run_inputs', type_='primary')
    op.create_primary_key('run_inputs_pkey', 'run_inputs', ['run_id', 's3_object_id'])
    
    op.drop_constraint('fk_run_inputs_s3_object_id_s3_objects', 'run_inputs', type_='foreignkey')
    op.create_foreign_key('run_inputs_s3_object_id_foreign', 'run_inputs', 's3_objects', ['s3_object_id'], ['object_key'])
    
    op.drop_constraint('fk_run_inputs_run_id_workflow_runs', 'run_inputs', type_='foreignkey')
    op.create_foreign_key('run_inputs_run_id_foreign', 'run_inputs', 'workflow_runs', ['run_id'], ['id'])
    
    # Revert run_metrics
    op.drop_constraint('fk_run_metrics_run_id_workflow_runs', 'run_metrics', type_='foreignkey')
    op.create_foreign_key('run_metrics_run_id_foreign', 'run_metrics', 'workflow_runs', ['run_id'], ['id'])
    
    # Revert s3_objects
    op.drop_constraint('uq_s3_objects_URI', 's3_objects', type_='unique')
    op.create_unique_constraint('s3_objects_uri_unique', 's3_objects', ['URI'])
    
    # Revert workflow_runs
    op.drop_constraint('fk_workflow_runs_workflow_id_workflows', 'workflow_runs', type_='foreignkey')
    op.create_foreign_key('workflow_runs_workflow_id_foreign', 'workflow_runs', 'workflows', ['workflow_id'], ['id'])
    
    op.drop_constraint('fk_workflow_runs_owner_user_id_app_users', 'workflow_runs', type_='foreignkey')
    op.create_foreign_key('workflow_runs_owner_user_id_foreign', 'workflow_runs', 'app_users', ['owner_user_id'], ['id'])
    
    op.drop_constraint('uq_workflow_runs_work_dir', 'workflow_runs', type_='unique')
    op.create_unique_constraint('workflow_runs_work_dir_unique', 'workflow_runs', ['work_dir'])
    
    op.drop_constraint('uq_workflow_runs_seqera_run_id', 'workflow_runs', type_='unique')
    op.create_unique_constraint('workflow_runs_seqera_run_id_unique', 'workflow_runs', ['seqera_run_id'])
    
    # Revert app_users
    op.drop_constraint('uq_app_users_email', 'app_users', type_='unique')
    op.create_unique_constraint('app_users_email_unique', 'app_users', ['email'])
    
    op.drop_constraint('uq_app_users_auth0_user_id', 'app_users', type_='unique')
    op.create_unique_constraint('app_users_auth0_user_id_unique', 'app_users', ['auth0_user_id'])

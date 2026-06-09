"""add_config_path_and_prerun_script_path_to_workflows

Adds config_path and prerun_script_path to the workflows table.
- config_path: Nextflow config file used at launch (e.g. interaction-screening).
- prerun_script_path: URL or local path to a shell script fetched at launch
  time and passed as the Seqera preRunScript. Dynamic variables (AWS
  credentials, S3 paths) are prepended as a header by the Python executor.
  URL validation is enforced at the application layer by fetch_workflow_config.

Both columns accept NULL, an absolute local path (starts with '/'), or a
clean HTTPS URL. URLs must not contain a 'token=' query parameter.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3ef9d934712f'
down_revision = '797ec472e447'
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_workflows_config_path_format"
_CHECK_SQL = (
    "(config_path IS NULL OR config_path LIKE 'https://%' OR config_path LIKE '/%')"
    " AND (prerun_script_path IS NULL"
    " OR prerun_script_path LIKE 'https://%'"
    " OR prerun_script_path LIKE '/%')"
)
_TOKEN_CHECK_NAME = "ck_workflows_config_path_no_token"
_TOKEN_CHECK_SQL = (
    "(config_path IS NULL OR config_path NOT LIKE '%token=%')"
    " AND (prerun_script_path IS NULL OR prerun_script_path NOT LIKE '%token=%')"
)


def upgrade() -> None:
    op.add_column('workflows', sa.Column('config_path', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('prerun_script_path', sa.Text(), nullable=True))
    op.create_check_constraint(_CHECK_NAME, 'workflows', _CHECK_SQL)
    op.create_check_constraint(_TOKEN_CHECK_NAME, 'workflows', _TOKEN_CHECK_SQL)


def downgrade() -> None:
    op.drop_constraint(_TOKEN_CHECK_NAME, 'workflows', type_='check')
    op.drop_constraint(_CHECK_NAME, 'workflows', type_='check')
    op.drop_column('workflows', 'prerun_script_path')
    op.drop_column('workflows', 'config_path')

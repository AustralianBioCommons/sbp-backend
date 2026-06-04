"""add_config_path_to_workflows

Adds config_path to the workflows table. Used by workflows that require a
Nextflow config file at launch time (e.g. interaction-screening). The value
is either an absolute local path (starts with '/') or an HTTPS URL (starts
with 'https://'). URLs must not contain a 'token=' query parameter — store
clean URLs and handle authentication separately.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3ef9d934712f'
down_revision = '797ec472e447'
branch_labels = None
depends_on = None

# config_path must be NULL, an absolute path, or a clean HTTPS URL.
# The token= check prevents accidentally persisting expiring GitHub raw tokens.
_CHECK_NAME = "ck_workflows_config_path_format"
_CHECK_SQL = (
    "config_path IS NULL"
    " OR config_path LIKE 'https://%'"
    " OR config_path LIKE '/%'"
)
_TOKEN_CHECK_NAME = "ck_workflows_config_path_no_token"
_TOKEN_CHECK_SQL = (
    "config_path IS NULL"
    " OR config_path NOT LIKE '%token=%'"
)


def upgrade() -> None:
    op.add_column('workflows', sa.Column('config_path', sa.Text(), nullable=True))
    op.create_check_constraint(_CHECK_NAME, 'workflows', _CHECK_SQL)
    op.create_check_constraint(_TOKEN_CHECK_NAME, 'workflows', _TOKEN_CHECK_SQL)


def downgrade() -> None:
    op.drop_constraint(_TOKEN_CHECK_NAME, 'workflows', type_='check')
    op.drop_constraint(_CHECK_NAME, 'workflows', type_='check')
    op.drop_column('workflows', 'config_path')

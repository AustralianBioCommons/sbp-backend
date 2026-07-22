"""drop_stale_name_only_unique_index_on_workflows"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7616ae331fe5'
down_revision = '7a377a05de6e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some environments have a plain unique index named 'uq_workflows_name' on
    # workflows.name alone, predating this feature and untracked by any prior
    # migration (likely created out-of-band). It collides with the
    # same-named (name, tool) constraint the next migration creates.
    op.execute("DROP INDEX IF EXISTS uq_workflows_name")


def downgrade() -> None:
    # The dropped index was untracked drift, not a schema feature; nothing to restore.
    pass

"""gadi_backend"""

from uuid import uuid7

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "98831ee0721c"
down_revision = "4077dfccd11c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    compute_backend = sa.Table("compute_backend", sa.MetaData(), autoload_with=op.get_bind())
    op.bulk_insert(
        compute_backend,
        [
            {
                "id": uuid7(),
                "name": "gadi",
                "enabled": True,
                # Populate these via DB admin
                "seqera_workspace_id": "",
                "seqera_compute_env_id": "",
                "env_prefix": "GADI_",
                "base_work_dir": "",
                "repository_transfer": "globus",
                "data_transfer": "globus",
                "service_usage_enabled": True,
            }
        ],
    )


def downgrade() -> None:
    compute_backend = sa.Table("compute_backend", sa.MetaData(), autoload_with=op.get_bind())
    op.execute(compute_backend.delete().where(compute_backend.c.name == "gadi"))

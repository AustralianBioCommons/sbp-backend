"""add_last_login_ip_to_app_users"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '91b54c7a79c5'
down_revision = 'c8d9e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_users", sa.Column("last_login_ip", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_users", "last_login_ip")

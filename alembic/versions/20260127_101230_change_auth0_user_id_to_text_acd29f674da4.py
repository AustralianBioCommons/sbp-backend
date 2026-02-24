"""change_auth0_user_id_to_text"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = 'acd29f674da4'
down_revision = '9a0a3098c22f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Change auth0_user_id from UUID to TEXT
    op.alter_column('app_users', 'auth0_user_id',
                    existing_type=postgresql.UUID(),
                    type_=sa.Text(),
                    existing_nullable=False)


def downgrade() -> None:
    # Revert auth0_user_id from TEXT back to UUID
    op.alter_column('app_users', 'auth0_user_id',
                    existing_type=sa.Text(),
                    type_=postgresql.UUID(),
                    existing_nullable=False)

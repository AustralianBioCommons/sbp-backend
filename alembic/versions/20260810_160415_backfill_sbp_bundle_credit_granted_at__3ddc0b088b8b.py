"""backfill sbp bundle credit granted at for existing users"""

from alembic import op

# revision identifiers, used by Alembic.
revision = '3ddc0b088b8b'
down_revision = '2aa8e5d81102'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # sbp_bundle_credit_granted_at was added as NULL for every row (see 2aa8e5d81102),
    # so left as-is every already-approved user would get an unintended retroactive
    # grant on their next request. The role itself isn't stored in this DB, but
    # having a workflow_runs row proves it was held (require_workflow_execution_role
    # gates the whole /workflows router) -- use that as the backfill signal. Approved
    # users with no runs yet stay NULL and still get their legitimate first grant.
    op.execute(
        """
        UPDATE app_users
        SET sbp_bundle_credit_granted_at = now()
        WHERE sbp_bundle_credit_granted_at IS NULL
          AND EXISTS (
              SELECT 1 FROM workflow_runs WHERE workflow_runs.owner_user_id = app_users.id
          )
        """
    )


def downgrade() -> None:
    # Backfilled timestamps can't be distinguished from genuine grants after the
    # fact, so there's nothing safe to revert here.
    pass

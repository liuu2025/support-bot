"""Associate refund proposals with the chat turn that created them."""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("refund_proposals", sa.Column("source_message_id", sa.Integer(), nullable=True))
    op.create_index("ix_refund_proposals_source_message_id", "refund_proposals", ["source_message_id"])


def downgrade():
    op.drop_index("ix_refund_proposals_source_message_id", table_name="refund_proposals")
    op.drop_column("refund_proposals", "source_message_id")

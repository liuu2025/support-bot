"""refund escalation lifecycle (human-in-the-loop)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28 04:00:00

Adds the reviewer-side lifecycle to `refunds`, so an escalated request is a
ticket that can be picked up and resolved instead of a dead-end row:

  escalated_at       when the agent handed it to a human
  escalation_status  pending -> approved | denied
  resolved_at        when a human decided
  resolved_by        reviewer identity (token `sub`)
  resolution_note    reviewer's note, shown to the customer when present

Existing escalated rows are backfilled as still-pending.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('refunds', sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('refunds', sa.Column('escalation_status', sa.String(), nullable=True))
    op.add_column('refunds', sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('refunds', sa.Column('resolved_by', sa.String(), nullable=True))
    op.add_column(
        'refunds',
        sa.Column('resolution_note', sa.String(), nullable=False, server_default=''),
    )
    op.create_index(
        op.f('ix_refunds_escalation_status'), 'refunds', ['escalation_status'], unique=False
    )

    # Backfill: anything the agent escalated before this migration is still
    # waiting for a human.
    op.execute(
        "UPDATE refunds SET escalation_status = 'pending', escalated_at = created_at "
        "WHERE decision = 'escalated'"
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_refunds_escalation_status'), table_name='refunds')
    op.drop_column('refunds', 'resolution_note')
    op.drop_column('refunds', 'resolved_by')
    op.drop_column('refunds', 'resolved_at')
    op.drop_column('refunds', 'escalation_status')
    op.drop_column('refunds', 'escalated_at')

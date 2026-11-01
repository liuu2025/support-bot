"""model service configuration (web-editable, OpenAI-compatible)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28 05:30:00

Replaces the env-only, per-provider model setup with a single OpenAI-compatible
endpoint that an admin edits in the UI:

  model_configs         one row (id=1): base_url + model_name + encrypted key;
                        `version` is bumped on save so cached clients rebuild.
  model_config_presets  saved endpoints, applied with one click.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'model_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('base_url', sa.String(length=512), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=False),
        sa.Column('api_key_encrypted', sa.String(), nullable=True),
        sa.Column('timeout_s', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'model_config_presets',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('base_url', sa.String(length=512), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=False),
        sa.Column('api_key_encrypted', sa.String(), nullable=True),
        sa.Column('timeout_s', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('model_config_presets')
    op.drop_table('model_configs')

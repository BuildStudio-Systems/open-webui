"""Add THERE domain bindings; preserve existing knowledge, skills and engine data.

Revision ID: e909a0010001
Revises: d4c1a8e37b62
"""

from alembic import op
import sqlalchemy as sa

revision = 'e909a0010001'
down_revision = 'd4c1a8e37b62'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'there_knowledge',
        sa.Column('id', sa.Text(), sa.ForeignKey('knowledge.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('engine_id', sa.Text(), nullable=True, unique=True),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'there_operation',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('resource_id', sa.Text(), nullable=True),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('user_id', 'request_id', name='uq_there_operation_request'),
    )
    op.create_index('ix_there_operation_user_id', 'there_operation', ['user_id'])
    op.create_index('ix_there_operation_resource_id', 'there_operation', ['resource_id'])
    op.create_table(
        'there_skill_origin',
        sa.Column('skill_id', sa.String(), sa.ForeignKey('skill.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('catalog_id', sa.Text(), nullable=False),
        sa.Column('digest', sa.String(80), nullable=False),
        sa.Column('version', sa.String(128), nullable=False),
        sa.Column('reviewed_by', sa.Text(), nullable=False),
        sa.Column('reviewed_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'there_paper',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('url', sa.String(2048), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.UniqueConstraint('user_id', 'url', name='uq_there_paper_user_url'),
    )
    op.create_index('ix_there_paper_user_id', 'there_paper', ['user_id'])


def downgrade():
    # Application rollback is supported by retaining these additive tables.
    # Dropping bindings could orphan private engine data, so never do it silently.
    raise RuntimeError('THERE data must be reconciled and backed up before a destructive schema downgrade')

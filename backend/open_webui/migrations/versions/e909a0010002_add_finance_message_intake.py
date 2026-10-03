"""Immutable There finance message receipts; no credential columns."""
from alembic import op
import sqlalchemy as sa

revision = 'e909a0010002'
down_revision = 'e909a0010001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('there_finance_intake',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('message_id', sa.Text(), nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=False),
        sa.Column('text_sha256', sa.Text(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('payload_sha256', sa.Text(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('receipt_json', sa.Text(), nullable=True),
        sa.UniqueConstraint('owner_id', 'message_id', name='uq_there_finance_message'))


def downgrade():
    raise RuntimeError('Keep additive intake receipts on application rollback; reconcile before destructive removal')

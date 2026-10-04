"""payments 增加 out_refund_no / refund_channel_no（退款对账用）

Revision ID: f3a9c1b2d4e5
Revises: 4edef1c70507
Create Date: 2026-10-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a9c1b2d4e5'
down_revision: Union[str, Sequence[str], None] = '4edef1c70507'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('out_refund_no', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_payments_out_refund_no'), ['out_refund_no'], unique=True)
        batch_op.add_column(sa.Column('refund_channel_no', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_payments_refund_channel_no'), ['refund_channel_no'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payments_refund_channel_no'))
        batch_op.drop_column('refund_channel_no')
        batch_op.drop_index(batch_op.f('ix_payments_out_refund_no'))
        batch_op.drop_column('out_refund_no')

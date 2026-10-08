"""Canvas late-policy fields on assignments (Brain Grade)

Revision ID: e6f4a0b3c5d8
Revises: d5e3f9a2b4c7
Create Date: 2026-10-08 12:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e6f4a0b3c5d8'
down_revision = 'd5e3f9a2b4c7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('assignment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('points_deducted', sa.Text(), nullable=True))  # EncryptedFloat
        batch_op.add_column(sa.Column('late_policy_status', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('assignment', schema=None) as batch_op:
        batch_op.drop_column('late_policy_status')
        batch_op.drop_column('points_deducted')

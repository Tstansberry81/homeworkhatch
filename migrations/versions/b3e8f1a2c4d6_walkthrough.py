"""The new-user walkthrough: where each student is in it and setup steps they've marked

Revision ID: b3e8f1a2c4d6
Revises: a9d3e7b1c2f4
Create Date: 2026-10-08 21:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b3e8f1a2c4d6'
down_revision = 'a9d3e7b1c2f4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tour_step', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('tour_done_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('tour_marks', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('tour_marks')
        batch_op.drop_column('tour_done_at')
        batch_op.drop_column('tour_step')

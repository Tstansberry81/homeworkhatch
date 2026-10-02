"""calendar events keep Canvas's all-day date; users remember extension copies from the zip

Revision ID: 5c1e0d7a9b42
Revises: 41d5a5920f9b
Create Date: 2026-10-02 19:30:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '5c1e0d7a9b42'
down_revision = '41d5a5920f9b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('calendar_event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('all_day', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('all_day_date', sa.Date(), nullable=True))

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('extension_ids', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('extension_ids')

    with op.batch_alter_table('calendar_event', schema=None) as batch_op:
        batch_op.drop_column('all_day_date')
        batch_op.drop_column('all_day')

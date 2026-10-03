"""calendar links from other LMSs, LMS diagnostics, and an lms column on accounts

Revision ID: b7d41e9a2c55
Revises: 8f3b2c6d1e07
Create Date: 2026-10-03 12:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b7d41e9a2c55'
down_revision = '8f3b2c6d1e07'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('canvas_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lms', sa.String(length=20), server_default='canvas', nullable=False))

    op.create_table(
        'calendar_feed',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('url_hash', sa.String(length=64), nullable=False),
        sa.Column('lms', sa.String(length=20), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('last_fetched_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column('etag', sa.String(length=300), nullable=True),
        sa.Column('event_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['canvas_account.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'url_hash'),
    )
    with op.batch_alter_table('calendar_feed', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_calendar_feed_user_id'), ['user_id'], unique=False)

    op.create_table(
        'lms_diagnostic',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('lms', sa.String(length=20), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('extension_version', sa.String(length=20), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('lms_diagnostic', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lms_diagnostic_user_id'), ['user_id'], unique=False)


def downgrade():
    op.drop_table('lms_diagnostic')
    op.drop_table('calendar_feed')
    with op.batch_alter_table('canvas_account', schema=None) as batch_op:
        batch_op.drop_column('lms')

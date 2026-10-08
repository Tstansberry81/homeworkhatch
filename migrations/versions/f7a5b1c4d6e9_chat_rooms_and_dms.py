"""automatic class chat rooms, enrollment roles, and private direct messages

Revision ID: f7a5b1c4d6e9
Revises: e6f4a0b3c5d8
Create Date: 2026-10-08 18:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f7a5b1c4d6e9'
down_revision = 'e6f4a0b3c5d8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chat_agreed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('birth_month', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('allow_dms', sa.Boolean(), server_default='1', nullable=False))
    with op.batch_alter_table('course', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chat_muted', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('enrollment_role', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('chat_key', sa.String(length=120), nullable=True))
        batch_op.create_index(batch_op.f('ix_course_chat_key'), ['chat_key'], unique=False)
    # Everyone (earlier chat joiners too) agrees once to the current rules, which now cover messages.

    op.create_table(
        'direct_thread',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_a_id', sa.Integer(), nullable=False),
        sa.Column('user_b_id', sa.Integer(), nullable=False),
        sa.Column('started_by', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('room_key', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_message_at', sa.DateTime(), nullable=False),
        sa.Column('last_sender_id', sa.Integer(), nullable=True),
        sa.Column('a_read_at', sa.DateTime(), nullable=True),
        sa.Column('b_read_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_a_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_b_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['started_by'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['last_sender_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_a_id', 'user_b_id'),
    )
    with op.batch_alter_table('direct_thread', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_direct_thread_user_a_id'), ['user_a_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_direct_thread_user_b_id'), ['user_b_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_direct_thread_last_message_at'), ['last_message_at'], unique=False)

    op.create_table(
        'direct_message',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('thread_id', sa.Integer(), nullable=False),
        sa.Column('sender_id', sa.Integer(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('deleted', sa.Boolean(), nullable=False),
        sa.Column('removed', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['thread_id'], ['direct_thread.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['sender_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('direct_message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_direct_message_thread_id'), ['thread_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_direct_message_sender_id'), ['sender_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_direct_message_created_at'), ['created_at'], unique=False)

    op.create_table(
        'user_block',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('blocker_id', sa.Integer(), nullable=False),
        sa.Column('blocked_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['blocker_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['blocked_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('blocker_id', 'blocked_id'),
    )
    with op.batch_alter_table('user_block', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_block_blocker_id'), ['blocker_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_block_blocked_id'), ['blocked_id'], unique=False)

    op.create_table(
        'direct_report',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('reporter_id', sa.Integer(), nullable=True),
        sa.Column('sender_id', sa.Integer(), nullable=True),
        sa.Column('sender_name', sa.String(length=40), nullable=True),
        sa.Column('sender_band', sa.String(length=10), nullable=True),
        sa.Column('snapshot', sa.Text(), nullable=True),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('resolved', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['direct_message.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reporter_id'], ['user.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['sender_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'reporter_id'),
    )
    with op.batch_alter_table('direct_report', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_direct_report_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_direct_report_sender_id'), ['sender_id'], unique=False)


def downgrade():
    op.drop_table('direct_report')
    op.drop_table('user_block')
    op.drop_table('direct_message')
    op.drop_table('direct_thread')
    with op.batch_alter_table('course', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_course_chat_key'))
        batch_op.drop_column('chat_key')
        batch_op.drop_column('enrollment_role')
        batch_op.drop_column('chat_muted')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('allow_dms')
        batch_op.drop_column('birth_month')
        batch_op.drop_column('chat_agreed_at')

"""Calendar items the student adds (from the tutor or by hand), and the tutor's suggested items

Revision ID: a9d3e7b1c2f4
Revises: f7a5b1c4d6e9
Create Date: 2026-10-08 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a9d3e7b1c2f4'
down_revision = 'f7a5b1c4d6e9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tutor_message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('calendar', sa.Text(), nullable=True))  # EncryptedJSON

    op.create_table(
        'user_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),  # EncryptedText
        sa.Column('notes', sa.Text(), nullable=True),  # EncryptedText
        sa.Column('start_at', sa.DateTime(), nullable=False),
        sa.Column('end_at', sa.DateTime(), nullable=True),
        sa.Column('all_day', sa.Boolean(), nullable=False),
        sa.Column('all_day_date', sa.Date(), nullable=True),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('done_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['course_id'], ['course.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['message_id'], ['tutor_message.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('user_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_event_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_event_start_at'), ['start_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_event_message_id'), ['message_id'], unique=False)


def downgrade():
    op.drop_table('user_event')
    with op.batch_alter_table('tutor_message', schema=None) as batch_op:
        batch_op.drop_column('calendar')

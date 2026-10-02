"""exam study planner: plans, sessions, "is this a test" choices; starred cards; decks and quizzes per exam

Revision ID: 8f3b2c6d1e07
Revises: 5c1e0d7a9b42
Create Date: 2026-10-02 21:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '8f3b2c6d1e07'
down_revision = '5c1e0d7a9b42'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'study_plan',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=True),
        sa.Column('assignment_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('exam_at', sa.DateTime(), nullable=True),
        sa.Column('tier', sa.String(length=10), nullable=False),
        sa.Column('share', sa.Float(), nullable=True),
        sa.Column('method', sa.String(length=20), nullable=False),
        sa.Column('pacing', sa.String(length=12), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('scope', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['course_id'], ['course.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['assignment_id'], ['assignment.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['event_id'], ['calendar_event.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('study_plan', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_study_plan_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_plan_assignment_id'), ['assignment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_plan_event_id'), ['event_id'], unique=False)

    op.create_table(
        'study_session',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('minutes', sa.Integer(), nullable=False),
        sa.Column('minutes_done', sa.Integer(), server_default='0', nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('done_at', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['plan_id'], ['study_plan.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('study_session', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_study_session_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_session_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_study_session_day'), ['day'], unique=False)

    op.create_table(
        'assessment_choice',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=True),
        sa.Column('item', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['course_id'], ['course.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'course_id', 'item'),
    )
    with op.batch_alter_table('assessment_choice', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assessment_choice_user_id'), ['user_id'], unique=False)

    op.create_table(
        'deck_test',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=True),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('seconds', sa.Integer(), nullable=True),
        sa.Column('answers', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['plan_id'], ['study_plan.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('deck_test', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deck_test_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_deck_test_plan_id'), ['plan_id'], unique=False)

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('study_minutes_per_day', sa.Integer(), server_default='120', nullable=False))
    with op.batch_alter_table('card', schema=None) as batch_op:
        batch_op.add_column(sa.Column('starred', sa.Boolean(), server_default='0', nullable=False))
    with op.batch_alter_table('deck', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_deck_plan_id'), ['plan_id'], unique=False)
        batch_op.create_foreign_key('fk_deck_plan_id', 'study_plan', ['plan_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('practice_quiz', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_practice_quiz_plan_id'), ['plan_id'], unique=False)
        batch_op.create_foreign_key('fk_practice_quiz_plan_id', 'study_plan', ['plan_id'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('practice_quiz', schema=None) as batch_op:
        batch_op.drop_constraint('fk_practice_quiz_plan_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_practice_quiz_plan_id'))
        batch_op.drop_column('plan_id')
    with op.batch_alter_table('deck', schema=None) as batch_op:
        batch_op.drop_constraint('fk_deck_plan_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_deck_plan_id'))
        batch_op.drop_column('plan_id')
    with op.batch_alter_table('card', schema=None) as batch_op:
        batch_op.drop_column('starred')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('study_minutes_per_day')
    op.drop_table('assessment_choice')
    op.drop_table('deck_test')
    op.drop_table('study_session')
    op.drop_table('study_plan')

"""opt-in sharing of decks and quizzes, card origins, share reports and strikes

Revision ID: c4d2e8f1a3b6
Revises: b7d41e9a2c55
Create Date: 2026-10-06 21:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c4d2e8f1a3b6'
down_revision = 'b7d41e9a2c55'
branch_labels = None
depends_on = None


def _share_columns(batch_op, table):
    batch_op.add_column(sa.Column('share_mode', sa.String(length=10), server_default='private', nullable=False))
    batch_op.add_column(sa.Column('share_token', sa.String(length=32), nullable=True))
    batch_op.add_column(sa.Column('shared_at', sa.DateTime(), nullable=True))
    batch_op.add_column(sa.Column('share_hidden', sa.Boolean(), server_default='0', nullable=False))
    batch_op.add_column(sa.Column('taken_down_at', sa.DateTime(), nullable=True))
    batch_op.add_column(sa.Column('copied_from_id', sa.Integer(), nullable=True))
    batch_op.create_unique_constraint(f'uq_{table}_share_token', ['share_token'])
    batch_op.create_index(batch_op.f(f'ix_{table}_copied_from_id'), ['copied_from_id'], unique=False)
    batch_op.create_foreign_key(f'fk_{table}_copied_from_id', table, ['copied_from_id'], ['id'], ondelete='SET NULL')


def upgrade():
    with op.batch_alter_table('deck', schema=None) as batch_op:
        _share_columns(batch_op, 'deck')
    with op.batch_alter_table('practice_quiz', schema=None) as batch_op:
        _share_columns(batch_op, 'practice_quiz')
        batch_op.add_column(sa.Column('share_blocked', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('from_deck_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('pasted_from', sa.String(length=10), nullable=True))
        batch_op.create_index(batch_op.f('ix_practice_quiz_from_deck_id'), ['from_deck_id'], unique=False)
        batch_op.create_foreign_key('fk_practice_quiz_from_deck_id', 'deck', ['from_deck_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('card', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.String(length=10), server_default='student', nullable=False))
        batch_op.add_column(sa.Column('origin_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('rewritten', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('share_block', sa.String(length=20), server_default='unchecked', nullable=True))
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('share_strikes', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('sharing_blocked', sa.Boolean(), server_default='0', nullable=False))

    # Cards the AI already made: from pasted notes ("Generated from Pasted notes") they're the student's
    # own material; from anything else (class files, pages, uploads) they're the AI's wording of class
    # materials, which is never shared until the student rewrites it.
    op.execute("UPDATE card SET origin = 'ai' WHERE deck_id IN "
               "(SELECT id FROM deck WHERE source = 'ai' AND description = 'Generated from Pasted notes')")
    op.execute("UPDATE card SET origin = 'ai_files' WHERE deck_id IN "
               "(SELECT id FROM deck WHERE source = 'ai' AND (description IS NULL OR description <> 'Generated from Pasted notes'))")

    op.create_table(
        'share_report',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('deck_id', sa.Integer(), nullable=True),
        sa.Column('quiz_id', sa.Integer(), nullable=True),
        sa.Column('reporter_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.String(length=20), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('snapshot', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('resolved', sa.Boolean(), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(['deck_id'], ['deck.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['quiz_id'], ['practice_quiz.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reporter_id'], ['user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('share_report', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_share_report_deck_id'), ['deck_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_share_report_quiz_id'), ['quiz_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_share_report_reporter_id'), ['reporter_id'], unique=False)
    with op.batch_alter_table('content_chunk', schema=None) as batch_op:
        batch_op.create_index('ix_content_chunk_user_source', ['user_id', 'source_type', 'source_id', 'ordinal'], unique=False)


def downgrade():
    with op.batch_alter_table('content_chunk', schema=None) as batch_op:
        batch_op.drop_index('ix_content_chunk_user_source')
    op.drop_table('share_report')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('sharing_blocked')
        batch_op.drop_column('share_strikes')
    with op.batch_alter_table('card', schema=None) as batch_op:
        batch_op.drop_column('share_block')
        batch_op.drop_column('rewritten')
        batch_op.drop_column('origin_text')
        batch_op.drop_column('origin')
    for table in ('practice_quiz', 'deck'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            if table == 'practice_quiz':
                batch_op.drop_constraint('fk_practice_quiz_from_deck_id', type_='foreignkey')
                batch_op.drop_index(batch_op.f('ix_practice_quiz_from_deck_id'))
                batch_op.drop_column('from_deck_id')
                batch_op.drop_column('pasted_from')
                batch_op.drop_column('share_blocked')
            batch_op.drop_constraint(f'fk_{table}_copied_from_id', type_='foreignkey')
            batch_op.drop_index(batch_op.f(f'ix_{table}_copied_from_id'))
            batch_op.drop_constraint(f'uq_{table}_share_token', type_='unique')
            for col in ('copied_from_id', 'taken_down_at', 'share_hidden', 'shared_at', 'share_token', 'share_mode'):
                batch_op.drop_column(col)

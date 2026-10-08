"""per-class customization: the student's own name, short code and color

Revision ID: d5e3f9a2b4c7
Revises: c4d2e8f1a3b6
Create Date: 2026-10-07 12:00:00

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd5e3f9a2b4c7'
down_revision = 'c4d2e8f1a3b6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('course', schema=None) as batch_op:
        batch_op.add_column(sa.Column('canvas_name', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('canvas_code', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('custom_name', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('custom_code', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('color', sa.String(length=7), nullable=True))
    # Until now name and course_code were always the LMS's.
    op.execute("UPDATE course SET canvas_name = name, canvas_code = course_code")


def downgrade():
    # Put the LMS's name back where a custom one was shown.
    op.execute("UPDATE course SET name = COALESCE(canvas_name, name), course_code = canvas_code")
    with op.batch_alter_table('course', schema=None) as batch_op:
        for col in ('color', 'custom_code', 'custom_name', 'canvas_code', 'canvas_name'):
            batch_op.drop_column(col)

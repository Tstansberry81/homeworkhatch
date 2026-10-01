"""application-level encryption

Revision ID: 41d5a5920f9b
Revises: 789c87c7f163
Create Date: 2026-10-01 12:30:51.532380

"""
from alembic import op
import hashlib
import re

import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '41d5a5920f9b'
down_revision = '789c87c7f163'
branch_labels = None
depends_on = None


# Columns whose values become encrypted text: (table, column, old type, nullable).
WIDEN = [
    ("activity_log", "detail", sa.String(500), True),
    ("assignment", "score", sa.Float(), True),
    ("assignment", "grade", sa.String(40), True),
    ("assignment", "rubric", sa.JSON(), True),
    ("assignment", "comments", sa.JSON(), True),
    ("assignment", "attachments", sa.JSON(), True),
    ("assignment", "rubric_assessment", sa.JSON(), True),
    ("calendar_event", "location", sa.String(300), True),
    ("canvas_account", "canvas_name", sa.String(200), True),
    ("course", "current_score", sa.Float(), True),
    ("course", "current_grade", sa.String(20), True),
    ("course", "final_score", sa.Float(), True),
    ("course", "final_grade", sa.String(20), True),
    ("practice_quiz", "questions", sa.JSON(), False),
    ("quiz_attempt", "answers", sa.JSON(), False),
    ("tutor_conversation", "title", sa.String(200), False),
    ("tutor_conversation", "attachments", sa.JSON(), True),
    ("tutor_message", "sources", sa.JSON(), True),
    ("user", "calendar_token", sa.String(64), False),
]
# Every encrypted column (including ones that were already TEXT), for the downgrade guard.
ENCRYPTED = WIDEN + [(t, c, None, True) for t, c in [
    ("course", "syllabus_html"), ("assignment", "description_html"), ("page", "body_html"),
    ("announcement", "message_html"), ("canvas_file", "text"), ("upload", "text"), ("content_chunk", "text"),
    ("card", "front"), ("card", "back"), ("summary", "content"), ("tutor_message", "content"),
    ("chat_message", "body")]]


def upgrade():
    op.create_table('app_state',
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('encryption_key',
    sa.Column('kid', sa.String(length=16), nullable=False),
    sa.Column('check', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('kid')
    )
    # Encrypted values are longer than the plaintext limits and aren't numbers or JSON anymore:
    # every such column becomes TEXT. Existing values keep their text form until the app encrypts them.
    tables = {}
    for table, column, old, nullable in WIDEN:
        tables.setdefault(table, []).append((column, old, nullable))
    for table, cols in tables.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column, old, nullable in cols:
                batch_op.alter_column(column, existing_type=old, type_=sa.Text(), existing_nullable=nullable,
                                      postgresql_using=f'"{column}"::text')

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('calendar_token_hash', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_user_calendar_token_hash'), ['calendar_token_hash'], unique=True)

    bind = op.get_bind()
    # Calendar feeds are looked up by a SHA-256 of the token from now on (the token itself gets encrypted).
    user = sa.table("user", sa.column("id", sa.Integer()), sa.column("calendar_token", sa.Text()),
                    sa.column("calendar_token_hash", sa.String()))
    for user_id, token in bind.execute(sa.select(user.c.id, user.c.calendar_token)).all():
        if token and not token.startswith("enc1:"):
            bind.execute(user.update().where(user.c.id == user_id)
                         .values(calendar_token_hash=hashlib.sha256(token.encode()).hexdigest()))
    # Coin history named each grade ("Grade bonus: HW 1 (93%)"); grades are encrypted now, so drop it.
    coin = sa.table("coin_transaction", sa.column("id", sa.Integer()), sa.column("reason", sa.String()))
    for coin_id, reason in bind.execute(sa.select(coin.c.id, coin.c.reason).where(coin.c.reason.like("Grade bonus:%(%"))).all():
        cleaned = re.sub(r"\s*\([0-9.]+%\)\s*$", "", reason)
        if cleaned != reason:
            bind.execute(coin.update().where(coin.c.id == coin_id).values(reason=cleaned))


def downgrade():
    bind = op.get_bind()
    for table, column, _old, _nullable in ENCRYPTED:
        t = sa.table(table, sa.column(column, sa.Text()))
        if bind.execute(sa.select(sa.func.count()).select_from(t).where(t.c[column].like("enc1:%"))).scalar():
            raise RuntimeError(f"{table}.{column} holds encrypted values: run `flask encryption decrypt-all --yes` "
                               "with the keys before downgrading, or the old code will show ciphertext.")
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_calendar_token_hash'))
        batch_op.drop_column('calendar_token_hash')
    tables = {}
    for table, column, old, nullable in WIDEN:
        tables.setdefault(table, []).append((column, old, nullable))
    for table, cols in tables.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column, old, nullable in cols:
                cast = {"FLOAT": "double precision", "JSON": "json"}.get(type(old).__name__.upper())
                batch_op.alter_column(column, existing_type=sa.Text(), type_=old, existing_nullable=nullable,
                                      postgresql_using=f'"{column}"::{cast}' if cast else None)
    op.drop_table('encryption_key')
    op.drop_table('app_state')

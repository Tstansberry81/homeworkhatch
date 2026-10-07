"""Migrations must never lose data. SQLite rebuilds a table to drop a column; with foreign keys on,
dropping the old `user` table would cascade-delete every class, file and set a student owns."""

from __future__ import annotations

import sqlite3

from flask_migrate import upgrade

from app import create_app


def test_sqlite_table_rebuilds_keep_child_rows(tmp_path):
    path = tmp_path / "hh.db"
    app = create_app("test", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}", "STORAGE_DIR": str(tmp_path / "storage")})
    with app.app_context():
        upgrade(revision="63eafcf4e6c6")  # just before the migrations that rebuild `user`
    con = sqlite3.connect(path)
    con.execute("insert into user (id, email, username, display_name, password_hash, is_admin, is_approved, active,"
                " created_at, timezone, onboarded, plan, plan_comped, gpa_scale, study_minutes_per_day, streak_days,"
                " show_on_leaderboards, calendar_token) values (1, 'a@b.c', 'a', 'A', 'x', 0, 1, 1, '2026-09-01',"
                " 'UTC', 1, 'free', 0, 4.0, 60, 0, 0, 'tok')")
    con.execute("insert into canvas_account (id, user_id, host, base_url, canvas_user_id)"
                " values (1, 1, 'canvas.test', 'https://canvas.test', '9')")
    con.execute("insert into course (id, user_id, account_id, canvas_id, name, class_key, room_key, hidden, active,"
                " files_tab_hidden, updated_at) values (1, 1, 1, '7', 'Calculus', 'k', 'r', 0, 1, 0, '2026-09-01')")
    con.commit()
    con.close()
    with app.app_context():
        upgrade()
    con = sqlite3.connect(path)
    assert con.execute("select name from course").fetchall() == [("Calculus",)]
    assert con.execute("pragma foreign_key_check").fetchall() == []


def test_sharing_migration_marks_existing_ai_cards(tmp_path):
    """AI cards made before sharing existed: from pasted notes they're the student's (origin "ai"); from
    anything else they're the AI's wording of class files ("ai_files"), held back from sharing."""
    path = tmp_path / "hh.db"
    app = create_app("test", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}", "STORAGE_DIR": str(tmp_path / "storage")})
    with app.app_context():
        upgrade(revision="b7d41e9a2c55")
    con = sqlite3.connect(path)
    con.execute("insert into user (id, email, username, display_name, password_hash, is_admin, is_approved, active,"
                " created_at, timezone, onboarded, plan, plan_comped, streak_days, show_on_leaderboards, calendar_token,"
                " session_version, study_minutes_per_day) values (1, 'a@b.c', 'a', 'A', 'x', 0, 1, 1, '2026-09-01', 'UTC', 1,"
                " 'free', 0, 0, 0, 'tok', 0, 120)")
    for deck_id, source, description in [(1, "ai", "Generated from Pasted notes"), (2, "ai", "Generated from week1.pdf"),
                                         (3, "manual", None), (4, "ai", None)]:
        con.execute("insert into deck (id, user_id, title, description, source, created_at) values (?, 1, 't', ?, ?, '2026-09-01')",
                    (deck_id, description, source))
        con.execute("insert into card (deck_id, front, back, position, ease, interval_days, repetitions, lapses, review_count,"
                    " due_at, starred) values (?, 'f', 'b', 0, 2.5, 0, 0, 0, 0, '2026-09-01', 0)", (deck_id,))
    con.commit()
    con.close()
    with app.app_context():
        upgrade()
    con = sqlite3.connect(path)
    rows = dict(con.execute("select deck_id, origin from card").fetchall())
    assert rows == {1: "ai", 2: "ai_files", 3: "student", 4: "ai_files"}
    assert {r[0] for r in con.execute("select share_block from card")} == {"unchecked"}
    assert {r[0] for r in con.execute("select share_mode from deck")} == {"private"}

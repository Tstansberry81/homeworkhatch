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

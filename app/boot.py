"""Startup step before gunicorn: run database migrations only when there's one to run.

`flask db upgrade` loads the whole app just to find out there's nothing to do, which on a small
instance adds many seconds to every wake-up. This compares the database's revision with the
migration scripts' head directly and only calls `flask db upgrade` when they differ (or when the
check itself can't tell).

    python -m app.boot && exec gunicorn wsgi:app ...
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def heads() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def current(url: str) -> set[str]:
    import psycopg

    from app.config import normalize_database_url

    libpq = normalize_database_url(url).replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(libpq, connect_timeout=20) as conn:
        return {row[0] for row in conn.execute("select version_num from alembic_version")}


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql")):
        try:
            if current(url) == heads():
                print("database is up to date; skipping migrations", flush=True)
                return 0
        except Exception as exc:  # anything odd: let the real migration command decide
            print(f"couldn't compare migration versions ({exc.__class__.__name__}); running them", flush=True)
    return subprocess.call([sys.executable, "-m", "flask", "db", "upgrade"], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())

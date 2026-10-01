"""Startup step before gunicorn: run database migrations only when there's one to run.

`flask db upgrade` loads the whole app just to find out there's nothing to do, which on a small
instance adds many seconds to every wake-up. This compares the database's revision with the
migration scripts' head directly and only calls `flask db upgrade` when they differ (or when the
check itself can't tell).

    python boot.py && exec gunicorn wsgi:app ...
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Standalone on purpose (and outside the app package, which would load with any import from it):
# loading the app or Alembic is what made this step slow.
ROOT = Path(__file__).resolve().parent
SUPABASE_ROOT_CA = ROOT / "app" / "certs" / "supabase-root-2021.crt"
_REV = re.compile(r"^revision\s*=\s*['\"]([0-9a-f]+)['\"]", re.M)
_DOWN = re.compile(r"^down_revision\s*=\s*(.+)$", re.M)


def heads() -> set[str]:
    """Revisions no other migration builds on."""
    revisions, parents = set(), set()
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text()
        rev = _REV.search(text)
        if rev:
            revisions.add(rev.group(1))
        down = _DOWN.search(text)
        if down:
            parents.update(re.findall(r"['\"]([0-9a-f]+)['\"]", down.group(1)))
    return revisions - parents


def libpq_url(url: str) -> str:
    """Same TLS rule as app.config.normalize_database_url: Supabase hosts are verified against
    Supabase's root CA unless the URL already sets sslmode."""
    for prefix in ("postgres://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix):]
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if "supabase" in (parts.hostname or "") and "sslmode" not in query:
        query.update(sslmode="verify-full", sslrootcert=str(SUPABASE_ROOT_CA))
    return urlunsplit(parts._replace(query=urlencode(query)))


def current(url: str) -> set[str]:
    import psycopg

    with psycopg.connect(libpq_url(url), connect_timeout=20) as conn:
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

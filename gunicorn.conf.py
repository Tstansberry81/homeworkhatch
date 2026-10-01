"""Gunicorn settings for Render.

Render sets GUNICORN_CMD_ARGS="--preload --bind=0.0.0.0:10000 ..." for Python services,
and that outranks this file for bind/preload, so bind is also given on the command line
in render.yaml. Hooks below still apply.
"""

import os

# One process with threads: on a fractional-CPU instance a second process adds no CPU, only
# memory (512 MB) and a second copy of every per-process cache. Requests mostly wait on the
# database, Claude and Composio, which threads handle well.
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
worker_class = "gthread"  # threads keep tutor streaming and uploads from blocking a worker
timeout = 180  # large Canvas files and AI answers can take a while
graceful_timeout = 30
accesslog = "-"


def post_fork(server, worker):
    """With --preload the app is imported before forking; never share pooled DB sockets."""
    from app.extensions import db
    from wsgi import app

    with app.app_context():
        db.engine.dispose(close=False)

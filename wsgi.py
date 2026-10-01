"""WSGI entry point: `gunicorn wsgi:app`."""

from app import create_app

app = create_app()

# gunicorn --preload imports this once before forking: load the AI SDK here so the first tutor
# question after a restart doesn't pay for it (it takes seconds on a small instance).
import anthropic  # noqa: E402,F401

"""Configuration, read from environment variables when the app is created.

Everything is built by `load_config()` at app-creation time (not at import time), so a
local `.env` file loaded by `create_app` is always honored.

Production target: Render (web) + Supabase (Postgres + Storage). See README "Deploy".
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- database


SUPABASE_ROOT_CA = BASE_DIR / "app" / "certs" / "supabase-root-2021.crt"


def normalize_database_url(url: str | None) -> str:
    """Accepts the connection strings Supabase/Render hand out and makes them SQLAlchemy-ready.

    - postgres:// and postgresql:// become postgresql+psycopg:// (SQLAlchemy 2 rejects the
      first, and a bare postgresql:// would pick psycopg2, which isn't installed).
    - Supabase hosts get sslmode=verify-full against Supabase's own root CA (bundled in
      app/certs; valid to 2031) unless an sslmode is already given: the connection is encrypted
      and the server has to prove it's really Supabase.
    """
    if not url:
        return f"sqlite:///{BASE_DIR / 'instance' / 'homeworkhatch.db'}"
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
            break
    if url.startswith("postgresql"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if "supabase" in (parts.hostname or "") and "sslmode" not in query:
            query["sslmode"] = "verify-full"
            query["sslrootcert"] = str(SUPABASE_ROOT_CA)
            url = urlunsplit(parts._replace(query=urlencode(query)))
    return url


def is_transaction_pooler(url: str) -> bool:
    """Supabase's Supavisor transaction mode listens on 6543 and can't use prepared statements."""
    try:
        return url.startswith("postgresql") and urlsplit(url).port == 6543
    except ValueError:
        return False


def engine_options(url: str) -> dict:
    if url.startswith("sqlite"):
        # Local development: wait for a busy database instead of failing immediately.
        return {"connect_args": {"timeout": 20}} if url != "sqlite://" else {}
    options: dict = {
        # Render resets long-lived outbound TCP connections on network changes; ping and
        # recycle so a dead connection is replaced instead of failing a request.
        "pool_pre_ping": True,
        "pool_recycle": env_int("DB_POOL_RECYCLE", 300),
    }
    if is_transaction_pooler(url):
        from sqlalchemy.pool import NullPool

        options["poolclass"] = NullPool
        options["connect_args"] = {"prepare_threshold": None}
    else:
        # Session-mode pooler / direct connection: a small pool per gunicorn worker. Keep
        # workers x (pool_size + max_overflow) under Supabase's pooler "Pool Size".
        options.update(pool_size=env_int("DB_POOL_SIZE", 6), max_overflow=env_int("DB_MAX_OVERFLOW", 4),
                       pool_timeout=30)
    return options


# ---------------------------------------------------------------- config objects


def env_pairs(name: str) -> dict:
    """ "a=1, b=2" -> {"a": "1", "b": "2"} """
    out = {}
    for part in (os.environ.get(name) or "").split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


def load_config(env_name: str) -> dict:
    database_url = normalize_database_url(os.environ.get("DATABASE_URL"))
    storage_backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    cfg = {
        "ENV_NAME": env_name,
        "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-insecure-change-me"),
        "SQLALCHEMY_DATABASE_URI": database_url,
        "SQLALCHEMY_ENGINE_OPTIONS": engine_options(database_url),
        # Public base URL, used for Stripe return URLs and the extension setup page.
        "PUBLIC_URL": (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/"),
        "GIT_COMMIT": os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None,

        # File storage: "local" (disk; development), "supabase" (Supabase Storage via its
        # S3-compatible API) or "s3" (AWS S3, Cloudflare R2, MinIO...).
        "STORAGE_BACKEND": storage_backend,
        "STORAGE_DIR": os.environ.get("STORAGE_DIR", str(BASE_DIR / "instance" / "storage")),
        "S3_BUCKET": os.environ.get("S3_BUCKET") or os.environ.get("SUPABASE_BUCKET", ""),
        "S3_ENDPOINT_URL": os.environ.get("S3_ENDPOINT_URL") or None,
        "S3_REGION": os.environ.get("S3_REGION") or os.environ.get("SUPABASE_S3_REGION") or None,
        "S3_ACCESS_KEY_ID": os.environ.get("S3_ACCESS_KEY_ID") or os.environ.get("SUPABASE_S3_ACCESS_KEY_ID")
                            or os.environ.get("AWS_ACCESS_KEY_ID"),
        "S3_SECRET_ACCESS_KEY": os.environ.get("S3_SECRET_ACCESS_KEY") or os.environ.get("SUPABASE_S3_SECRET_ACCESS_KEY")
                                or os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "SUPABASE_URL": supabase_url,
        # Downloads are served as short-lived signed URLs from object storage.
        "DOWNLOAD_URL_TTL": env_int("DOWNLOAD_URL_TTL", 300),
        # The extension uploads files straight to storage with presigned URLs valid this long
        # (a first sync of a big semester can take a while).
        "UPLOAD_URL_TTL": env_int("UPLOAD_URL_TTL", 3600),
        # Read text from uploaded files in the request instead of a background thread (tests).
        "EXTRACT_INLINE": env_bool("EXTRACT_INLINE", False),

        "MAX_SNAPSHOT_MB": env_int("MAX_SNAPSHOT_MB", 64),
        # Supabase's Free plan caps each file at 50 MB; raise this with a paid plan.
        # Supabase caps objects at 50 MB; encryption adds a little, so plaintext stops at 49.
        "MAX_FILE_MB": env_int("MAX_FILE_MB", 49 if storage_backend == "supabase" else 512),
        # Text is extracted from uploaded files up to this size for the AI tutor and generators.
        "MAX_EXTRACT_MB": env_int("MAX_EXTRACT_MB", 40),

        # AI. The Anthropic SDK reads ANTHROPIC_API_KEY itself; AI features are hidden without it.
        "AI_MODEL": os.environ.get("AI_MODEL", "claude-opus-5-5"),
        # Per-feature models; each falls back to AI_MODEL. Kinds: tutor, flashcards, quiz,
        # summary, transcribe. AI_MODELS="tutor=claude-haiku-4-5,flashcards=claude-sonnet-5-5".
        "AI_MODELS": env_pairs("AI_MODELS"),
        "AI_ENABLED": env_bool("AI_ENABLED", True),

        # Billing (optional). Without STRIPE_SECRET_KEY every account stays on the free plan.
        "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY", ""),
        "STRIPE_WEBHOOK_SECRET": os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        "STRIPE_PRICE_PASS": os.environ.get("STRIPE_PRICE_PASS", ""),  # one-time price, $20
        "STRIPE_PRICE_PLUS": os.environ.get("STRIPE_PRICE_PLUS", ""),  # recurring monthly price, $6


        # Chrome extension ids the Connect Canvas page links automatically: the Web Store id
        # first (its store page is the install link), then any unpacked/dev ids.
        "EXTENSION_IDS": [x.strip() for x in os.environ.get("EXTENSION_IDS", "plnbpopfekhdcbllmdcmkjgcemikllch").split(",")
                          if x.strip()],

        # Google Calendar + Google Drive through Composio (https://composio.dev). Without a key
        # the integrations are hidden. Auth configs are found or created automatically;
        # COMPOSIO_AUTH_CONFIGS="googlecalendar=ac_...,googledrive=ac_..." pins them, and
        # COMPOSIO_TOOLKIT_VERSIONS="googlecalendar=20260101_00,..." pins tool versions.
        "COMPOSIO_API_KEY": os.environ.get("COMPOSIO_API_KEY", "").strip(),
        "COMPOSIO_AUTH_CONFIGS": env_pairs("COMPOSIO_AUTH_CONFIGS"),
        "COMPOSIO_TOOLKIT_VERSIONS": env_pairs("COMPOSIO_TOOLKIT_VERSIONS"),

        # Application-level encryption (see SECURITY.md): "kid:base64key,..." with the active key
        # first. Required in production; generated into instance/ for local development.
        "ENCRYPTION_KEYS": os.environ.get("ENCRYPTION_KEYS", "").strip(),
        # On once every existing value is encrypted: unencrypted values are then refused.
        "ENCRYPTION_STRICT": env_bool("ENCRYPTION_STRICT", False),

        # Shown on /support (and used as the Chrome Web Store support contact). Without it the
        # page points to GitHub issues.
        "SUPPORT_EMAIL": os.environ.get("SUPPORT_EMAIL", "").strip(),
        # The DMCA designated agent as registered at copyright.gov/dmca-directory: name, mailing address,
        # phone and email, one per line ("\n" in the env var). Shown on the Copyright policy page.
        "DMCA_AGENT": os.environ.get("DMCA_AGENT", "").replace("\\n", "\n").strip(),
        # A form for suggestions and bug reports (e.g. a Google Form), linked from Account and
        # Support. Only https links are shown.
        "FEEDBACK_URL": (lambda u: u if u.startswith("https://") else "")(os.environ.get("FEEDBACK_URL", "").strip()),
        # The address to print in links people keep or pass on (calendar feed, shared sets, live-game joins):
        # the custom domain on Render, where both addresses serve the same site. Sign-in flows keep using
        # the address in use, since login cookies are per address.
        "CANONICAL_URL": (os.environ.get("CANONICAL_URL") or ("https://homeworkhatch.com" if os.environ.get("RENDER") else "")).rstrip("/"),
        # Render's own address for this service. It keeps working after a custom domain is added,
        # and extensions too old to know the custom domain can only link from it.
        "RENDER_URL": os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/"),

        # The account registered with this email becomes an admin (production has no
        # "first user is admin" shortcut). `flask create-admin` works too.
        "ADMIN_EMAIL": os.environ.get("ADMIN_EMAIL", ""),
        # New accounts wait for an admin to approve them.
        "REQUIRE_APPROVAL": env_bool("REQUIRE_APPROVAL", False),
        # Probability Lab with virtual coins: 18+ only, off unless enabled.
        "FEATURE_SIMULATIONS": env_bool("FEATURE_SIMULATIONS", False),

        # Static files: cached by browsers for a year (URLs carry the deploy's commit, see url_defaults).
        "SEND_FILE_MAX_AGE_DEFAULT": 31536000,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_HTTPONLY": True,
        "WTF_CSRF_TIME_LIMIT": None,
    }
    if storage_backend == "supabase" and not cfg["S3_ENDPOINT_URL"] and supabase_url:
        # https://<ref>.supabase.co -> https://<ref>.storage.supabase.co/storage/v1/s3 (the
        # storage host is Supabase's recommended endpoint for large transfers).
        host = urlsplit(supabase_url).hostname or ""
        ref = host.split(".")[0]
        cfg["S3_ENDPOINT_URL"] = f"https://{ref}.storage.supabase.co/storage/v1/s3" if host.endswith(".supabase.co") \
            else f"{supabase_url}/storage/v1/s3"
    if env_name == "production":
        cfg.update(SESSION_COOKIE_SECURE=True, REMEMBER_COOKIE_SECURE=True, PREFERRED_URL_SCHEME="https")
    if env_name == "test":
        test_url = os.environ.get("TEST_DATABASE_URL")  # set to run the suite against Postgres
        cfg.update(TESTING=True, SECRET_KEY="test", WTF_CSRF_ENABLED=False, AI_ENABLED=True, STORAGE_BACKEND="local",
                   ENCRYPTION_KEYS=TEST_ENCRYPTION_KEYS, ENCRYPTION_STRICT=False,
                   FEATURE_SIMULATIONS=False, REQUIRE_APPROVAL=False, EXTRACT_INLINE=True, COMPOSIO_API_KEY="",
                   SQLALCHEMY_DATABASE_URI=normalize_database_url(test_url) if test_url else "sqlite://",
                   SQLALCHEMY_ENGINE_OPTIONS=engine_options(normalize_database_url(test_url)) if test_url else {})
    return cfg


# A fixed key for the test suite only (never used anywhere else).
TEST_ENCRYPTION_KEYS = "t1:" + "dGVzdC1vbmx5LWtleS1kby1ub3QtdXNlLWFueXdoZXJlIQ"[:43]


def validate_production(cfg: dict) -> list[str]:
    """Problems that would make a production deploy unsafe or broken."""
    problems = []
    if not cfg.get("ENCRYPTION_KEYS"):
        problems.append("ENCRYPTION_KEYS is not set (production data must be encrypted).")
    if cfg["SECRET_KEY"] == "dev-insecure-change-me":
        problems.append("SECRET_KEY is not set.")
    if cfg["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        problems.append("DATABASE_URL is not set (SQLite on Render is wiped on every deploy).")
    if cfg["STORAGE_BACKEND"] == "local":
        problems.append("STORAGE_BACKEND is local (Render's disk is wiped on every deploy); use supabase or s3.")
    if cfg["STORAGE_BACKEND"] in {"supabase", "s3"}:
        for key in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            if not cfg.get(key):
                problems.append(f"{key} is not set for {cfg['STORAGE_BACKEND']} storage.")
        if cfg["STORAGE_BACKEND"] == "supabase" and not cfg["S3_ENDPOINT_URL"]:
            problems.append("SUPABASE_URL (or S3_ENDPOINT_URL) is not set for Supabase storage.")
    return problems

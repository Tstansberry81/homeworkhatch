import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return f"sqlite:///{BASE_DIR / 'instance' / 'homeworkhatch.db'}"
    # Render/Heroku hand out postgres:// URLs; SQLAlchemy wants an explicit driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    ENV_NAME = os.environ.get("HH_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Public base URL, used for Stripe return URLs and the extension setup page.
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

    # File storage for synced Canvas files: "local" (disk) or "s3" (S3, R2, MinIO...).
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
    STORAGE_DIR = os.environ.get("STORAGE_DIR", str(BASE_DIR / "instance" / "storage"))
    S3_BUCKET = os.environ.get("S3_BUCKET", "")
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None
    S3_REGION = os.environ.get("S3_REGION") or None

    MAX_SNAPSHOT_MB = int(os.environ.get("MAX_SNAPSHOT_MB", "64"))
    MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "512"))
    # Text is extracted from uploaded files up to this size for the AI tutor and generators.
    MAX_EXTRACT_MB = int(os.environ.get("MAX_EXTRACT_MB", "40"))

    # AI. The Anthropic SDK reads ANTHROPIC_API_KEY itself; AI features are disabled without it.
    AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5-5")
    AI_ENABLED = _bool("AI_ENABLED", True)

    # Billing (optional). Without STRIPE_SECRET_KEY every account stays on the free plan.
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PRICE_NORMAL = os.environ.get("STRIPE_PRICE_NORMAL", "")
    STRIPE_PRICE_PREMIUM = os.environ.get("STRIPE_PRICE_PREMIUM", "")
    STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "")

    # College acceptance calculator: free key from https://api.data.gov/signup/
    COLLEGE_SCORECARD_API_KEY = os.environ.get("COLLEGE_SCORECARD_API_KEY", "")

    # New accounts wait for an admin to approve them.
    REQUIRE_APPROVAL = _bool("REQUIRE_APPROVAL", False)
    # Probability-game "simulations" with virtual coins: 18+ only, off unless enabled.
    FEATURE_SIMULATIONS = _bool("FEATURE_SIMULATIONS", False)

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    WTF_CSRF_TIME_LIMIT = None


class ProductionConfig(Config):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test"
    # Set TEST_DATABASE_URL to run the suite against Postgres.
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    WTF_CSRF_ENABLED = False
    AI_ENABLED = True
    STORAGE_BACKEND = "local"


def config_for(env_name: str):
    return {"production": ProductionConfig, "test": TestConfig}.get(env_name, Config)

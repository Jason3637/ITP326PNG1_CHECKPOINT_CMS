"""Application configuration loaded from environment variables.

Values are read from the process environment. ``python-dotenv`` loads a
local ``.env`` file into that environment during development (see
``app/__init__.py``). Nothing secret is hard-coded here.
"""

import os
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.pool import StaticPool


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.environ.get(name, default))


class Config:
    """Base config shared by every environment."""

    # --- Core Flask ---
    SECRET_KEY = os.environ.get("SECRET_KEY", os.environ.get("JWT_SECRET_KEY", "dev-secret-change-me"))
    FLASK_ENV = os.environ.get("FLASK_ENV", "production")
    DEBUG = FLASK_ENV == "development"

    # --- Database (Supabase Postgres) ---
    # DATABASE_URL is required for real use. During Phase A scaffolding the
    # app must still boot so /api/docs can be verified, so fall back to a
    # local throwaway SQLite file when it is unset.
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///scaffold_placeholder.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Supabase poolers and IPv4 add-ons behave better with pre-ping + recycle.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # --- JWT ---
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret-change-me")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

    # --- MFA / TOTP ---
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt users.totp_secret
    # at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    MFA_ENCRYPTION_KEY = os.environ.get("MFA_ENCRYPTION_KEY", "")
    TOTP_ISSUER = os.environ.get("TOTP_ISSUER", "Prime's Vault")
    # Short-lived tokens that gate the two-step MFA flow (not full access tokens).
    MFA_SETUP_TOKEN_EXPIRES = timedelta(minutes=15)
    MFA_CHALLENGE_TOKEN_EXPIRES = timedelta(minutes=5)
    BACKUP_CODE_COUNT = 10

    # --- Supabase Storage ---
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "")
    # Max upload size and how long download links stay valid.
    DOCUMENT_MAX_BYTES = int(os.environ.get("DOCUMENT_MAX_BYTES", str(10 * 1024 * 1024)))
    SIGNED_URL_EXPIRY_SECONDS = int(os.environ.get("SIGNED_URL_EXPIRY_SECONDS", "600"))

    # --- Flask-RESTX / Swagger ---
    RESTX_MASK_SWAGGER = False
    SWAGGER_UI_DOC_EXPANSION = "list"

    # --- Lending parameters -------------------------------------------------
    # Prime's Vault is a small SME, not a bank with tiered rate products, so a
    # single configurable default rate lives in the environment (not a rate
    # table). A loan officer may still override the rate per-decision; whatever
    # rate is actually used is persisted on the Loan row.
    # These are the *seed* defaults. At runtime an admin can override any of
    # them via PUT /api/admin/parameters (stored in the system_parameters
    # table); read them through app.services.parameters, not straight off config.
    DEFAULT_ANNUAL_INTEREST_RATE = _decimal("DEFAULT_ANNUAL_INTEREST_RATE", "0.18")  # 18% APR
    MIN_LOAN_AMOUNT = _decimal("MIN_LOAN_AMOUNT", "100")
    MAX_LOAN_AMOUNT = _decimal("MAX_LOAN_AMOUNT", "50000")
    MAX_LOAN_TERM_MONTHS = int(os.environ.get("MAX_LOAN_TERM_MONTHS", "60"))
    MIN_LOAN_TERM_MONTHS = int(os.environ.get("MIN_LOAN_TERM_MONTHS", "1"))
    # --- Credit evaluation (interim model - see app/services/credit_evaluation.py) ---
    # Provisional guesses, NOT confirmed by the client. Tune via
    # PUT /api/admin/parameters once real underwriting figures are provided.
    MIN_MONTHLY_INCOME = _decimal("MIN_MONTHLY_INCOME", "200")  # PGK
    MAX_DEBT_TO_INCOME_RATIO = _decimal("MAX_DEBT_TO_INCOME_RATIO", "0.40")  # 40%
    # Days before an installment's due date to send the "repayment due soon" email.
    REPAYMENT_REMINDER_LEAD_DAYS = int(os.environ.get("REPAYMENT_REMINDER_LEAD_DAYS", "3"))
    # ISO currency code, for the frontend to format amounts (Prime's Vault is in PNG).
    CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "PGK")

    # --- CORS (separately-hosted Next.js frontend) ------------------------
    # Comma-separated list of allowed origins. Dev defaults cover the Next.js
    # dev server; set CORS_ORIGINS to the deployed frontend URL(s) in prod.
    CORS_ORIGINS = [
        o.strip()
        for o in os.environ.get(
            "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
        ).split(",")
        if o.strip()
    ]

    # --- Email / Notifications (Zoho Mail SMTP) ---------------------------
    # NOTIFICATIONS ONLY - never used for auth codes (see app/services/notifications.py).
    NOTIFICATIONS_ENABLED = _bool(os.environ.get("NOTIFICATIONS_ENABLED"), False)
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.zoho.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USE_TLS = _bool(os.environ.get("SMTP_USE_TLS"), True)
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USERNAME", ""))
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Prime's Vault")
    MAIL_TIMEOUT_SECONDS = int(os.environ.get("MAIL_TIMEOUT_SECONDS", "10"))


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    DEBUG = False
    # Self-contained: an in-memory SQLite DB shared across connections, a fixed
    # (throwaway) crypto key, and email disabled. Never touches Supabase.
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    JWT_SECRET_KEY = "testing-only-not-a-real-secret-0123456789"
    MFA_ENCRYPTION_KEY = "ZVa21zYrbaUc1mZIHOQNMEUBYCw63cU504ux-rk9xXU="  # test-only Fernet key
    NOTIFICATIONS_ENABLED = False
    CORS_ORIGINS = ["http://localhost:3000"]


_CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    """Return the config class for ``name`` (falls back to FLASK_ENV)."""
    name = (name or os.environ.get("FLASK_ENV") or "production").lower()
    return _CONFIGS.get(name, ProductionConfig)

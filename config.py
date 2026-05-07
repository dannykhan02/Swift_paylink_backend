"""
config.py
─────────────────────────────────────────────────────────────
Centralised configuration for the PayPal Payment System.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


# ══════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════

def _resolve_database_url() -> str:
    external_url = os.getenv("EXTERNAL_DATABASE_URL")
    database_url = os.getenv("DATABASE_URL", "sqlite:///payments.db")
    internal_url = os.getenv("INTERNAL_DATABASE_URL")

    selected = external_url or database_url or internal_url

    if not selected:
        raise ValueError(
            "No database URL found. "
            "Set DATABASE_URL or EXTERNAL_DATABASE_URL in your .env file."
        )

    if selected == external_url:
        print("🔗 Using EXTERNAL_DATABASE_URL")
    elif selected == database_url:
        print("🔗 Using DATABASE_URL")
    else:
        print("🔗 Using INTERNAL_DATABASE_URL (fallback)")

    if selected.startswith("postgres://"):
        selected = selected.replace("postgres://", "postgresql://", 1)
        print("🔄 Fixed postgres:// → postgresql://")

    return selected


def _prepare_database_url(url: str) -> str:
    if not url or "sqlite" in url:
        return url
    if "sslmode=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sslmode=prefer&connect_timeout=30"


def _build_engine_options(url: str) -> dict:
    if "sqlite" in url:
        return {}
    return {
        "pool_size":     5,
        "max_overflow":  10,
        "pool_timeout":  30,
        "pool_recycle":  3600,
        "pool_pre_ping": True,
        "connect_args": {
            "sslmode":          "prefer",
            "connect_timeout":  30,
            "application_name": "paypal_payment_system",
        },
    }


def _parse_cors_origins() -> list[str]:
    """
    Build the CORS allow-list.

    Production:  reads CORS_ORIGINS (comma-separated) from .env, then
                 unconditionally adds FRONTEND_URL.
    Development: also adds common localhost ports so `npm run dev` works
                 without touching .env.
    """
    raw      = os.getenv("CORS_ORIGINS", "")
    frontend = os.getenv("FRONTEND_URL", "http://localhost:8080")
    is_prod  = os.getenv("FLASK_ENV", "development").lower() == "production" \
               or os.getenv("RAILWAY_ENVIRONMENT") is not None  # Railway sets this

    # Start from whatever is explicitly configured
    origins: list[str] = []
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]

    # Always include the canonical frontend origin
    if frontend not in origins:
        origins.append(frontend)

    # Only add localhost wildcards in non-production environments
    if not is_prod:
        defaults = [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
        ]
        for origin in defaults:
            if origin not in origins:
                origins.append(origin)

    return origins


# ══════════════════════════════════════════════════════════════
#  CONFIG CLASS
# ══════════════════════════════════════════════════════════════

class Config:
    # ── Flask ────────────────────────────────────────────────
    SECRET_KEY: str  = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
    DEBUG:      bool = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    PORT:       int  = int(os.getenv("PORT", 5000))

    # ── JWT ──────────────────────────────────────────────────
    JWT_SECRET_KEY:           str = os.getenv("JWT_SECRET_KEY", SECRET_KEY)
    JWT_ACCESS_TOKEN_EXPIRES: int = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRES", 60 * 60 * 8))

    # ── PayPal ───────────────────────────────────────────────
    PAYPAL_CLIENT_ID:     str  = os.getenv("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET: str  = os.getenv("PAYPAL_CLIENT_SECRET", "")
    IS_SANDBOX:           bool = os.getenv("PAYPAL_SANDBOX", "true").lower() == "true"

    PAYPAL_BASE_URL: str = (
        "https://api-m.sandbox.paypal.com"
        if IS_SANDBOX
        else "https://api-m.paypal.com"
    )

    # ── URLs ─────────────────────────────────────────────────
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:8080")
    RETURN_URL:   str = os.getenv("RETURN_URL",   "http://localhost:5000/success")
    CANCEL_URL:   str = os.getenv("CANCEL_URL",   "http://localhost:5000/cancel")

    # ── Email ────────────────────────────────────────────────
    SMTP_EMAIL:    str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")

    # ── Database ─────────────────────────────────────────────
    _raw_db_url: str = _prepare_database_url(_resolve_database_url())

    DATABASE_URL:   str  = _raw_db_url
    ENGINE_OPTIONS: dict = _build_engine_options(_raw_db_url)
    IS_SQLITE:      bool = "sqlite" in _raw_db_url

    # ── CORS ─────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = _parse_cors_origins()

    # ── SQLAlchemy ────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI:        str  = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    SQLALCHEMY_ENGINE_OPTIONS:      dict = ENGINE_OPTIONS

    # ── Helpers ───────────────────────────────────────────────
    @classmethod
    def masked_db_url(cls) -> str:
        url = cls.DATABASE_URL
        return url.split("@")[0] + "@***" if "@" in url else url

    @classmethod
    def validate(cls) -> None:
        warnings: list[str] = []
        if not cls.PAYPAL_CLIENT_ID:
            warnings.append("PAYPAL_CLIENT_ID is not set")
        if not cls.PAYPAL_CLIENT_SECRET:
            warnings.append("PAYPAL_CLIENT_SECRET is not set")
        if cls.SECRET_KEY == "dev-secret-change-in-production":
            warnings.append("SECRET_KEY is using the insecure default — change it!")
        if not cls.SMTP_EMAIL:
            warnings.append("SMTP_EMAIL is not set — confirmation emails will be skipped")
        for w in warnings:
            print(f"⚠️  Config warning: {w}")

    @classmethod
    def summary(cls) -> None:
        mode = "sandbox" if cls.IS_SANDBOX else "live"
        print("=" * 50)
        print("💳 PAYPAL PAYMENT SYSTEM v1.0")
        print(f"🔧 PayPal mode : {mode}")
        print(f"🗄️  Database    : {cls.masked_db_url()}")
        print(f"🌐 Frontend    : {cls.FRONTEND_URL}")
        print(f"🐛 Debug       : {cls.DEBUG}")
        print(f"🔀 CORS origins: {', '.join(cls.CORS_ORIGINS)}")
        print(f"🔐 JWT expires : {cls.JWT_ACCESS_TOKEN_EXPIRES // 3600}h")
        print("📡 Endpoints   : /api/pay  /api/payments  /api/dashboard")
        print("=" * 50)


# ── Resolve at import time ────────────────────────────────────
try:
    print(f"✅ Database URL configured: {Config.masked_db_url()}")
    Config.validate()
except Exception as exc:
    print(f"❌ Configuration error: {exc}")
    sys.exit(1)
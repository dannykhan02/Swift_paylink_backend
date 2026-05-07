"""
app.py
─────────────────────────────────────────────────────────────
Main Flask application entry point.
"""

import sys
import time

from flask import Flask, request, redirect, jsonify
from flask_cors import CORS
from flask_restful import Api
import requests as http

from config import Config
from models import db, migrate
from resources import register_resources
from admin_resources import register_admin_resources
from paypal import get_access_token, capture_paypal_order
from email_utils import send_email
from auth import auth_bp, jwt


# ══════════════════════════════════════════════════════════════
#  APP FACTORY
# ══════════════════════════════════════════════════════════════

def create_app() -> Flask:
    app = Flask(__name__)

    # ── Flask / SQLAlchemy config ────────────────────────────
    app.config["SECRET_KEY"]                     = Config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"]        = Config.SQLALCHEMY_DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = Config.SQLALCHEMY_TRACK_MODIFICATIONS
    app.config["SQLALCHEMY_ENGINE_OPTIONS"]      = Config.SQLALCHEMY_ENGINE_OPTIONS

    # ── JWT ──────────────────────────────────────────────────
    app.config["JWT_SECRET_KEY"]              = Config.JWT_SECRET_KEY
    app.config["JWT_ACCESS_TOKEN_EXPIRES"]    = Config.JWT_ACCESS_TOKEN_EXPIRES
    jwt.init_app(app)

    db.init_app(app)
    migrate.init_app(app, db)

    # ── CORS ─────────────────────────────────────────────────
    # Config.CORS_ORIGINS is built from CORS_ORIGINS env var + FRONTEND_URL.
    # In production (Railway sets RAILWAY_ENVIRONMENT) localhost origins
    # are NOT added — only what's explicitly in the .env is allowed.
    CORS(
        app,
        origins=Config.CORS_ORIGINS,
        supports_credentials=True,
        expose_headers=["Set-Cookie"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )

    # ── Public API ────────────────────────────────────────────
    public_api = Api(app, prefix="/api")
    register_resources(public_api)

    # ── Admin API ─────────────────────────────────────────────
    admin_api = Api(app, prefix="/api")
    register_admin_resources(admin_api)

    # ── Auth blueprint ────────────────────────────────────────
    app.register_blueprint(auth_bp, url_prefix="/api/auth")

    # ── PayPal redirect routes ────────────────────────────────
    app.add_url_rule("/success", "success", success_handler)
    app.add_url_rule("/cancel",  "cancel",  cancel_handler)

    # ── Health endpoints ─────────────────────────────────────
    @app.route("/")
    def root_health():
        return {
            "status":    "healthy",
            "message":   "PayPal Payment System is running",
            "timestamp": time.time(),
            "version":   "1.0",
        }, 200

    @app.route("/health")
    def detailed_health():
        return {
            "status":    "ok",
            "timestamp": time.time(),
            "version":   "1.0",
        }, 200

    # ── Debug endpoints ───────────────────────────────────────
    @app.route("/debug/config")
    def debug_config():
        client_id = Config.PAYPAL_CLIENT_ID
        secret    = Config.PAYPAL_CLIENT_SECRET

        def preview(value: str, length: int = 6) -> str:
            if not value:
                return "<not set>"
            return value[:length] + "..." if len(value) > length else value

        payload = {
            "client_id_set":         bool(client_id),
            "client_id_preview":     preview(client_id),
            "secret_set":            bool(secret),
            "secret_preview":        preview(secret),
            "is_sandbox":            Config.IS_SANDBOX,
            "base_url":              Config.PAYPAL_BASE_URL,
            "frontend_url":          Config.FRONTEND_URL,
            "return_url":            Config.RETURN_URL,
            "cancel_url":            Config.CANCEL_URL,
            "smtp_email_set":        bool(Config.SMTP_EMAIL),
            "secret_key_is_default": (Config.SECRET_KEY == "dev-secret-change-in-production"),
            "database_url_preview":  Config.masked_db_url(),
            "cors_origins":          Config.CORS_ORIGINS,   # ← added for easier debugging
        }
        payload["all_credentials_set"] = all([
            payload["client_id_set"],
            payload["secret_set"],
            not payload["secret_key_is_default"],
        ])
        return jsonify(payload), 200

    @app.route("/debug/routes")
    def debug_routes():
        routes = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
            routes.append({
                "endpoint": rule.endpoint,
                "methods":  sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}),
                "path":     rule.rule,
            })
        return jsonify(routes), 200

    # ── Error handlers ────────────────────────────────────────
    @app.errorhandler(500)
    def internal_error(error):
        return {"error": "Internal server error", "status": 500}, 500

    @app.errorhandler(404)
    def not_found(error):
        return {
            "error":  "Resource not found",
            "status": 404,
            "endpoints": {
                "pay":              "/api/pay",
                "payments":         "/api/admin/payments",
                "payments_bulk":    "/api/admin/payments/bulk",
                "payments_export":  "/api/admin/payments/export",
                "health":           "/api/health",
                "dashboard":        "/api/admin/dashboard",
                "invoice_generate": "/api/invoice/generate",
                "invoice_records":  "/api/admin/invoices",
                "debug_config":     "/debug/config",
                "debug_routes":     "/debug/routes",
            },
        }, 404

    @app.errorhandler(403)
    def forbidden(error):
        return {"error": "Access forbidden", "status": 403}, 403

    return app


# ══════════════════════════════════════════════════════════════
#  DATABASE HELPERS
# ══════════════════════════════════════════════════════════════

def test_database_connection(app: Flask, max_retries: int = 3, retry_delay: int = 2) -> bool:
    for attempt in range(max_retries):
        try:
            with app.app_context():
                with db.engine.connect() as conn:
                    conn.execute(db.text("SELECT 1"))
            return True
        except Exception as e:
            print(f"DB connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2
    return False


def initialize_app(app: Flask) -> bool:
    print("🚀 Starting application initialization...")
    max_retries = 5

    for attempt in range(max_retries):
        delay = 2 * (2 ** attempt)
        try:
            with app.app_context():
                print(f"🔄 Attempt {attempt + 1}/{max_retries}...")
                if not test_database_connection(app):
                    raise Exception("DB connection failed")
                print("✅ Database connected")
                db.create_all()
                print("✅ Tables created/verified")
                print("🎉 Initialization complete!")
                return True
        except Exception as e:
            print(f"❌ Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                print(f"⏳ Retrying in {delay}s...")
                time.sleep(delay)

    print("❌ All initialization attempts failed")
    return False


# ══════════════════════════════════════════════════════════════
#  PAYPAL REDIRECT HANDLERS
# ══════════════════════════════════════════════════════════════

def success_handler():
    from models import Payment, CheckoutSession, InvoiceRecord
    from models import SessionStatus, PaymentStatus

    order_id = request.args.get("token")

    if not order_id:
        return redirect(f"{Config.FRONTEND_URL}?error=missing_order_id")

    try:
        token    = get_access_token()
        captured = capture_paypal_order(token, order_id)

        payer       = captured.get("payer", {})
        payer_name  = (
            f"{payer.get('name', {}).get('given_name', '')} "
            f"{payer.get('name', {}).get('surname', '')}".strip()
        )
        payer_email = payer.get("email_address", "")
        payer_id    = payer.get("payer_id", "")

        units      = captured.get("purchase_units", [{}])
        unit       = units[0] if units else {}
        captures   = unit.get("payments", {}).get("captures", [{}])
        capture    = captures[0] if captures else {}
        amount_obj = capture.get("amount", {})
        capture_id = capture.get("id", "")
        description = unit.get("description", "")

        session        = CheckoutSession.query.filter_by(order_id=order_id).first()
        invoice_number = session.invoice_number if session else None
        funding_source = session.funding_source if session else None

        payment = Payment(
            order_id=order_id,
            invoice_number=invoice_number,
            amount=amount_obj.get("value", "0.00"),
            currency=amount_obj.get("currency_code", "USD"),
            description=description,
            status=PaymentStatus.PAID,
            payer_email=payer_email,
            payer_name=payer_name,
            payer_id=payer_id,
            capture_id=capture_id,
            funding_source=funding_source,
        )
        db.session.add(payment)
        db.session.flush()

        if session and session.invoice_record:
            session.invoice_record.payment_id = payment.id
        elif invoice_number:
            inv_record = InvoiceRecord.query.filter_by(
                invoice_number=invoice_number
            ).first()
            if inv_record and inv_record.payment_id is None:
                inv_record.payment_id = payment.id

        if session:
            session.status = SessionStatus.COMPLETED

        db.session.commit()

        if payer_email:
            send_email(payer_email, order_id, payment.amount, payment.currency)

        return redirect(f"{Config.FRONTEND_URL}?success=true&order_id={order_id}")

    except http.exceptions.HTTPError as exc:
        print(f"[PayPal] Capture error: {exc}")
        return redirect(f"{Config.FRONTEND_URL}?error=capture_failed")
    except Exception as exc:
        print(f"[Success] Unexpected error: {exc}")
        return redirect(f"{Config.FRONTEND_URL}?error=server_error")


def cancel_handler():
    from models import CheckoutSession, SessionStatus

    order_id = request.args.get("token")

    if order_id:
        session = CheckoutSession.query.filter_by(order_id=order_id).first()
        if session:
            session.status = SessionStatus.CANCELLED
            db.session.commit()

    return redirect(f"{Config.FRONTEND_URL}?cancelled=true")


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

app = create_app()

if __name__ == "__main__":
    print("🏃 Running in development mode")
    initialize_app(app)
    Config.summary()
    app.run(
        debug=Config.DEBUG,
        host="0.0.0.0",
        port=Config.PORT,
    )
else:
    print("🏭 Running in production mode")
    try:
        initialize_app(app)
        print("✅ Production initialization complete")
    except Exception as e:
        print(f"⚠️ Initialization warning: {e}")
    Config.summary()
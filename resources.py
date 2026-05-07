"""
resources.py
─────────────────────────────────────────────────────────────
Public Flask-RESTful API resources for the PayPal payment system.

Admin endpoints have been moved entirely to admin_resources.py.
This file registers ONLY the three public routes:
  GET  /api/health
  GET  /api/invoice/generate
  POST /api/pay

All former admin classes (PaymentListResource, PaymentResource,
InvoiceRecordListResource, InvoiceRecordResource, SessionListResource,
SessionResource, EmailLogListResource, EmailLogResource,
DashboardResource, InvoiceCleanupResource) have been removed.
They are superseded by the richer implementations in admin_resources.py
which are registered via register_admin_resources() in app.py.
"""

import time
import hmac
import hashlib
from datetime import datetime
from functools import wraps

from flask import request
from flask_restful import Resource
from flask_jwt_extended import verify_jwt_in_request, get_jwt
import requests as http

from config import Config
from models import (
    db, Payment, CheckoutSession, EmailLog, InvoiceCounter, InvoiceRecord,
    SessionStatus, PaymentStatus, FundingSource, EmailStatus,
)
from paypal import get_access_token, create_paypal_order
from email_utils import send_email

# How long a preview invoice token stays valid (seconds).
INVOICE_TOKEN_TTL_SECONDS = 30 * 60  # 30 minutes


# ══════════════════════════════════════════════════════════════
#  INVOICE HELPERS
# ══════════════════════════════════════════════════════════════

def _hmac_digest(payload: str) -> str:
    return hmac.new(
        Config.SECRET_KEY.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def _invoice_number_suffix(raw: str) -> str:
    return _hmac_digest(raw)[:4].upper()


def _build_invoice_number(year: int, seq: int) -> str:
    raw = f"{year}-{seq:05d}"
    return f"INV-{raw}-{_invoice_number_suffix(raw)}"


def verify_invoice_number(invoice_number: str) -> bool:
    try:
        parts = invoice_number.split("-")
        if len(parts) != 4 or parts[0] != "INV":
            return False
        year_str, seq_str, stored_suffix = parts[1], parts[2], parts[3]
        raw = f"{year_str}-{seq_str}"
        return hmac.compare_digest(_invoice_number_suffix(raw), stored_suffix)
    except Exception:
        return False


# ── Preview token helpers ─────────────────────────────────────────────────────

def _make_preview_token(year: int, seq: int) -> str:
    expires_ts = int(time.time()) + INVOICE_TOKEN_TTL_SECONDS
    payload = f"{year}|{seq}|{expires_ts}"
    sig = _hmac_digest(payload)
    return f"{payload}|{sig}"


def _verify_preview_token(token: str) -> tuple[bool, int, int]:
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return False, 0, 0
        year_str, seq_str, expires_str, stored_sig = parts
        payload = f"{year_str}|{seq_str}|{expires_str}"
        if not hmac.compare_digest(_hmac_digest(payload), stored_sig):
            return False, 0, 0
        if int(time.time()) > int(expires_str):
            return False, 0, 0
        return True, int(year_str), int(seq_str)
    except Exception:
        return False, 0, 0


# ══════════════════════════════════════════════════════════════
#  PUBLIC RESOURCES
# ══════════════════════════════════════════════════════════════

class HealthResource(Resource):
    def get(self):
        return {
            "status":    "ok",
            "sandbox":   Config.IS_SANDBOX,
            "mode":      "sandbox" if Config.IS_SANDBOX else "live",
            "version":   "1.0",
            "timestamp": time.time(),
        }, 200


class InvoiceGeneratorResource(Resource):
    def get(self):
        year = datetime.utcnow().year
        counter = InvoiceCounter.query.filter_by(year=year).first()
        next_seq = (counter.last_seq if counter else 0) + 1
        invoice_number = _build_invoice_number(year, next_seq)
        preview_token = _make_preview_token(year, next_seq)
        return {
            "invoice_number": invoice_number,
            "preview_token":  preview_token,
            "expires_in":     INVOICE_TOKEN_TTL_SECONDS,
            "year":           year,
            "sequence":       next_seq,
        }, 200


class PayResource(Resource):
    # ── Official PayPal REST API supported currencies only ────────────────────
    # Source: https://developer.paypal.com/api/rest/reference/currency-codes/
    # Currencies outside this set will be rejected by PayPal with a 422.
    #
    # Notes from PayPal docs:
    #   ¹ HUF, JPY, TWD — do NOT support decimals; pass whole numbers only.
    #   ² BRL — in-country Brazilian PayPal accounts only.
    #   ³ CNY, MYR — in-country accounts only (CN / MY respectively).
    _SUPPORTED_CURRENCIES = {
        "AUD",  # Australian dollar
        "BRL",  # Brazilian real ²
        "CAD",  # Canadian dollar
        "CNY",  # Chinese Renminbi ³
        "CZK",  # Czech koruna
        "DKK",  # Danish krone
        "EUR",  # Euro
        "GBP",  # Pound sterling
        "HKD",  # Hong Kong dollar
        "HUF",  # Hungarian forint ¹ (no decimals)
        "ILS",  # Israeli new shekel
        "JPY",  # Japanese yen ¹ (no decimals)
        "MXN",  # Mexican peso
        "MYR",  # Malaysian ringgit ³
        "NOK",  # Norwegian krone
        "NZD",  # New Zealand dollar
        "PHP",  # Philippine peso
        "PLN",  # Polish złoty
        "RUB",  # Russian ruble
        "SEK",  # Swedish krona
        "SGD",  # Singapore dollar
        "CHF",  # Swiss franc
        "THB",  # Thai baht
        "TWD",  # New Taiwan dollar ¹ (no decimals)
        "USD",  # United States dollar
    }

    # Currencies that do not support decimal amounts — must be whole numbers.
    _NO_DECIMAL_CURRENCIES = {"HUF", "JPY", "TWD"}

    def post(self):
        data = request.get_json(force=True)
        amount         = str(data.get("amount", "0.00"))
        currency       = str(data.get("currency", "USD")).upper()
        description    = str(data.get("description", ""))
        funding_source = str(data.get("funding_source", "paypal")).lower()
        preview_token  = data.get("preview_token")

        try:
            float_amount = float(amount)
            if float_amount <= 0:
                return {"error": "Amount must be greater than 0"}, 400
        except ValueError:
            return {"error": "Invalid amount format"}, 400

        if currency not in self._SUPPORTED_CURRENCIES:
            return {
                "error":                f"Unsupported currency: {currency}",
                "supported_currencies": sorted(self._SUPPORTED_CURRENCIES),
            }, 400

        # Enforce whole-number amounts for currencies that don't support decimals.
        if currency in self._NO_DECIMAL_CURRENCIES:
            amount = str(int(float_amount))

        if funding_source not in {"paypal", "card"}:
            funding_source = "paypal"

        if preview_token:
            valid, _, _ = _verify_preview_token(preview_token)
            if not valid:
                return {
                    "error": (
                        "Your invoice preview has expired or is invalid. "
                        "Please refresh the page and try again."
                    )
                }, 400
        else:
            print("⚠️  POST /pay called without preview_token — proceeding")

        try:
            year = datetime.utcnow().year
            counter = (
                InvoiceCounter.query
                .filter_by(year=year)
                .with_for_update()
                .first()
            )
            if counter is None:
                counter = InvoiceCounter(year=year, last_seq=0)
                db.session.add(counter)
                db.session.flush()
            counter.last_seq += 1
            real_seq = counter.last_seq

            invoice_number  = _build_invoice_number(year, real_seq)
            pay_description = description or invoice_number

            token = get_access_token()
            order = create_paypal_order(
                token, amount, currency,
                pay_description, funding_source,
            )
            approval_url = next(
                (link["href"] for link in order.get("links", [])
                 if link.get("rel") == "payer-action"),
                None,
            )
            if not approval_url:
                db.session.rollback()
                return {"error": "PayPal did not return an approval URL"}, 502

            record = InvoiceRecord(
                invoice_number=invoice_number,
                year=year,
                sequence=real_seq,
                session_id=None,
                payment_id=None,
            )
            db.session.add(record)

            session = CheckoutSession(
                order_id=order["id"],
                invoice_number=invoice_number,
                amount=amount,
                currency=currency,
                description=pay_description,
                funding_source=FundingSource(funding_source),
                status=SessionStatus.INITIATED,
            )
            db.session.add(session)
            db.session.flush()

            record.session_id = session.id
            db.session.commit()

            return {
                "approval_url":   approval_url,
                "order_id":       order["id"],
                "invoice_number": invoice_number,
            }, 201

        except http.exceptions.HTTPError as exc:
            db.session.rollback()
            return {"error": f"PayPal API error: {exc}"}, 502
        except Exception as exc:
            db.session.rollback()
            return {"error": str(exc)}, 500


# ══════════════════════════════════════════════════════════════
#  REGISTRATION HELPER  — public routes ONLY
# ══════════════════════════════════════════════════════════════

def register_resources(api) -> None:
    """
    Register public API resources (no auth required).

    Admin resources are registered separately via
    register_admin_resources() in admin_resources.py, called from app.py.

    Previously this function also registered the following admin classes,
    which have been removed to eliminate route conflicts:
      - PaymentListResource       → /api/admin/payments         (admin_resources.py)
      - PaymentResource           → /api/admin/payments/<id>    (admin_resources.py)
      - InvoiceRecordListResource → /api/admin/invoices         (admin_resources.py)
      - InvoiceRecordResource     → /api/admin/invoices/<num>   (admin_resources.py)
      - SessionListResource       → /api/admin/sessions         (admin_resources.py)
      - SessionResource           → /api/admin/sessions/<id>    (admin_resources.py)
      - EmailLogListResource      → /api/admin/email-logs       (admin_resources.py)
      - EmailLogResource          → /api/admin/email-logs/<id>  (admin_resources.py)
      - DashboardResource         → /api/admin/dashboard        (admin_resources.py)
      - InvoiceCleanupResource    → /api/admin/invoice/cleanup  (admin_resources.py)
    """
    api.add_resource(HealthResource,           "/health")
    api.add_resource(InvoiceGeneratorResource, "/invoice/generate")
    api.add_resource(PayResource,              "/pay")
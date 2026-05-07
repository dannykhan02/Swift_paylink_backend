"""
admin_resources.py
─────────────────────────────────────────────────────────────
Scalable admin-only API resources for the PayPal payment system.

All endpoints require JWT admin token:
    Authorization: Bearer <token>

Endpoints
─────────
  GET  /api/admin/payments                  — paginated, filtered, sorted payments
  GET  /api/admin/payments/export           — export all matching payments as JSON
  POST /api/admin/payments/bulk             — bulk status update or delete
  GET  /api/admin/payments/<order_id>       — single payment detail
  PATCH /api/admin/payments/<order_id>      — update single payment status
  DELETE /api/admin/payments/<order_id>     — delete single payment

  GET  /api/admin/sessions                  — paginated sessions
  GET  /api/admin/sessions/<order_id>       — single session

  GET  /api/admin/invoices                  — paginated invoice records
  GET  /api/admin/invoices/<invoice_number> — single invoice record
  POST /api/admin/invoices/cleanup          — delete old orphaned invoice records

  GET  /api/admin/email-logs                — paginated email logs
  GET  /api/admin/email-logs/<order_id>     — email logs for one order

  GET  /api/admin/dashboard                 — full dashboard summary
  GET  /api/admin/stats                     — lightweight stats (no payment list)

  GET  /api/admin/profile                   — current admin profile
  PATCH /api/admin/profile                  — update admin full_name
  POST /api/admin/admins                    — create a new admin (JWT required)
  GET  /api/admin/admins                    — list all admins

Pagination
──────────
  All list endpoints accept:
    ?page=1&per_page=25       (default per_page=25, max=100)

  Response envelope:
    {
      "data":       [...],
      "pagination": {
        "page": 1, "per_page": 25, "total": 1042,
        "pages": 42, "has_next": true, "has_prev": false
      }
    }

Filtering & Sorting (payments)
──────────────────────────────
  ?status=PAID|FAILED|REFUNDED
  ?currency=USD|KES|EUR|GBP
  ?funding_source=paypal|card
  ?search=<order_id or invoice_number or payer_email>
  ?date_from=YYYY-MM-DD
  ?date_to=YYYY-MM-DD
  ?sort_by=created_at|amount|payer_email   (default: created_at)
  ?sort_dir=desc|asc                       (default: desc)

Bulk Actions (POST /api/admin/payments/bulk)
────────────────────────────────────────────
  Body:
    {
      "action":    "delete" | "status_update",
      "order_ids": ["ABC", "DEF", ...],          // specific IDs, OR
      "select_all": true,                         // apply to ALL matching
      "filters":   { ... same as GET filters },   // used with select_all
      "status":    "FAILED"                       // required for status_update
    }
"""

import math
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from flask import request
from flask_restful import Resource
from flask_jwt_extended import verify_jwt_in_request, get_jwt, get_jwt_identity
from sqlalchemy import or_, asc, desc, func, cast, Float, Integer
from sqlalchemy import Column                           # used only for isinstance checks
from sqlalchemy.orm.attributes import InstrumentedAttribute as _IA

from models import (
    db, Payment, CheckoutSession, EmailLog,
    InvoiceRecord, AdminUser,
    SessionStatus, PaymentStatus, EmailStatus, FundingSource,
)

# ---------------------------------------------------------------------------
# Pylance fix: obtain properly-typed Column proxies for every attribute we
# filter/sort/search on.  SQLAlchemy mapped attributes ARE InstrumentedAttribute
# objects at runtime (they support .ilike, .is_(), .isnot(), .in_(), etc.), but
# Pylance infers them as their Python scalar type (str, int …) from the
# db.Column definition.  Aliasing them here through __dict__ with an explicit
# annotation gives Pylance a typed handle without any `type: ignore` comments.
# ---------------------------------------------------------------------------

# Payment column proxies
_pay_order_id:       _IA = Payment.__dict__["order_id"]
_pay_invoice_number: _IA = Payment.__dict__["invoice_number"]
_pay_payer_email:    _IA = Payment.__dict__["payer_email"]
_pay_payer_name:     _IA = Payment.__dict__["payer_name"]
_pay_status:         _IA = Payment.__dict__["status"]
_pay_currency:       _IA = Payment.__dict__["currency"]
_pay_funding_source: _IA = Payment.__dict__["funding_source"]
_pay_created_at:     _IA = Payment.__dict__["created_at"]
_pay_amount:         _IA = Payment.__dict__["amount"]

# InvoiceRecord column proxies
_inv_session_id: _IA = InvoiceRecord.__dict__["session_id"]
_inv_payment_id: _IA = InvoiceRecord.__dict__["payment_id"]
_inv_issued_at:  _IA = InvoiceRecord.__dict__["issued_at"]

# CheckoutSession column proxies
_ses_status:     _IA = CheckoutSession.__dict__["status"]
_ses_created_at: _IA = CheckoutSession.__dict__["created_at"]

# EmailLog column proxies
_email_status:   _IA = EmailLog.__dict__["status"]
_email_sent_at:  _IA = EmailLog.__dict__["sent_at"]
_email_order_id: _IA = EmailLog.__dict__["order_id"]


# ══════════════════════════════════════════════════════════════
#  AUTH DECORATOR
# ══════════════════════════════════════════════════════════════

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            verify_jwt_in_request()
        except Exception as exc:
            return {"error": "Authorization required", "detail": str(exc)}, 401
        claims = get_jwt()
        if claims.get("role") != "admin":
            return {"error": "Admin access required"}, 403
        return fn(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════
#  PAGINATION HELPER
# ══════════════════════════════════════════════════════════════

def paginate(query, page: int, per_page: int) -> dict:
    """
    Execute a paginated SQLAlchemy query.
    Returns { items: [...], pagination: {...} }
    """
    per_page = min(per_page, 100)   # hard cap
    per_page = max(per_page, 1)
    page     = max(page, 1)

    total = query.count()
    pages = math.ceil(total / per_page) if total > 0 else 1
    items = query.offset((page - 1) * per_page).limit(per_page).all()

    return {
        "items": items,
        "pagination": {
            "page":     page,
            "per_page": per_page,
            "total":    total,
            "pages":    pages,
            "has_next": page < pages,
            "has_prev": page > 1,
        },
    }


# ══════════════════════════════════════════════════════════════
#  PAYMENT FILTER / SORT BUILDER  (reads from request.args)
# ══════════════════════════════════════════════════════════════

def _build_payment_query():
    """
    Read query-string params from the current request and return
    a filtered + sorted SQLAlchemy query for Payment.
    """
    args = request.args

    status         = (args.get("status",         "") or "").upper()  or None
    currency       = (args.get("currency",       "") or "").upper()  or None
    funding_source = (args.get("funding_source", "") or "").lower()  or None
    search         = (args.get("search",         "") or "").strip()  or None
    date_from      = args.get("date_from")  or None
    date_to        = args.get("date_to")    or None
    sort_by        = args.get("sort_by",  "created_at")
    sort_dir       = (args.get("sort_dir", "desc") or "desc").lower()

    return _apply_payment_filters(
        Payment.query,
        status=status,
        currency=currency,
        funding_source=funding_source,
        search=search,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


def _apply_payment_filters(
    q,
    *,
    status:         Optional[str] = None,
    currency:       Optional[str] = None,
    funding_source: Optional[str] = None,
    search:         Optional[str] = None,
    date_from:      Optional[str] = None,
    date_to:        Optional[str] = None,
    sort_by:  str = "created_at",
    sort_dir: str = "desc",
):
    """
    Core filter/sort logic shared by _build_payment_query() and
    _build_payment_query_from_dict().  All column accesses use the
    __dict__ proxies so Pylance resolves them as InstrumentedAttribute.
    """
    # ── status ────────────────────────────────────────────────
    if status:
        try:
            q = q.filter(_pay_status == PaymentStatus(status))
        except ValueError:
            pass

    # ── currency ──────────────────────────────────────────────
    if currency:
        q = q.filter(_pay_currency == currency)

    # ── funding source ────────────────────────────────────────
    if funding_source:
        try:
            q = q.filter(_pay_funding_source == FundingSource(funding_source))
        except ValueError:
            pass

    # ── text search ───────────────────────────────────────────
    if search:
        pattern = f"%{search}%"
        q = q.filter(
            or_(
                _pay_order_id.ilike(pattern),
                _pay_invoice_number.ilike(pattern),
                _pay_payer_email.ilike(pattern),
                _pay_payer_name.ilike(pattern),
            )
        )

    # ── date range ────────────────────────────────────────────
    if date_from:
        try:
            q = q.filter(_pay_created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass

    if date_to:
        try:
            q = q.filter(_pay_created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
        except ValueError:
            pass

    # ── sort ──────────────────────────────────────────────────
    sort_column_map = {
        "created_at":  _pay_created_at,
        "amount":      _pay_amount,
        "payer_email": _pay_payer_email,
        "status":      _pay_status,
        "currency":    _pay_currency,
    }
    sort_col = sort_column_map.get(sort_by, _pay_created_at)
    q = q.order_by(desc(sort_col) if sort_dir == "desc" else asc(sort_col))

    return q


def _build_payment_query_from_dict(filters: dict):
    """
    Same logic as _build_payment_query() but reads from a plain dict
    (used by the bulk endpoint's select_all path).
    """
    status         = (filters.get("status",         "") or "").upper()  or None
    currency       = (filters.get("currency",       "") or "").upper()  or None
    funding_source = (filters.get("funding_source", "") or "").lower()  or None
    search         = (filters.get("search",         "") or "").strip()  or None
    date_from      = filters.get("date_from")  or None
    date_to        = filters.get("date_to")    or None
    sort_by        = filters.get("sort_by",  "created_at") or "created_at"
    sort_dir       = (filters.get("sort_dir", "desc") or "desc").lower()

    return _apply_payment_filters(
        Payment.query,
        status=status,
        currency=currency,
        funding_source=funding_source,
        search=search,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


# ══════════════════════════════════════════════════════════════
#  DASHBOARD  (admin only)
# ══════════════════════════════════════════════════════════════

class AdminDashboardResource(Resource):
    @admin_required
    def get(self):
        """Full dashboard with summary + recent 25 payments."""
        payments = Payment.query.order_by(desc(_pay_created_at)).all()
        sessions = CheckoutSession.query.all()

        paid_count     = sum(1 for p in payments if p.status == PaymentStatus.PAID)
        failed_count   = sum(1 for p in payments if p.status == PaymentStatus.FAILED)
        refunded_count = sum(1 for p in payments if p.status == PaymentStatus.REFUNDED)

        revenue_by_currency: dict[str, float] = {}
        for p in payments:
            if p.status == PaymentStatus.PAID:
                revenue_by_currency[p.currency] = (
                    revenue_by_currency.get(p.currency, 0.0) + float(p.amount)
                )

        email_sent   = EmailLog.query.filter(_email_status == EmailStatus.SENT).count()
        email_failed = EmailLog.query.filter(_email_status == EmailStatus.FAILED).count()

        orphaned_invoices = InvoiceRecord.query.filter(
            _inv_session_id.is_(None)
        ).count()
        abandoned_invoices = InvoiceRecord.query.filter(
            _inv_session_id.isnot(None),
            _inv_payment_id.is_(None),
        ).count()

        return {
            "summary": {
                "total_payments":    len(payments),
                "paid_payments":     paid_count,
                "failed_payments":   failed_count,
                "refunded_payments": refunded_count,
                "revenue_by_currency": {
                    k: round(v, 2) for k, v in revenue_by_currency.items()
                },
                "sessions": {
                    "initiated": sum(1 for s in sessions if s.status == SessionStatus.INITIATED),
                    "completed": sum(1 for s in sessions if s.status == SessionStatus.COMPLETED),
                    "cancelled": sum(1 for s in sessions if s.status == SessionStatus.CANCELLED),
                },
                "emails": {
                    "sent":   email_sent,
                    "failed": email_failed,
                },
                "invoices": {
                    "orphaned":  orphaned_invoices,
                    "abandoned": abandoned_invoices,
                },
                "last_payment": payments[0].to_dict() if payments else None,
            },
            # Only last 25 in the dashboard view; use GET /api/admin/payments
            # for the full paginated list.
            "recent_payments": [p.to_dict() for p in payments[:25]],
        }, 200


# ══════════════════════════════════════════════════════════════
#  STATS  (admin only, lightweight — no payment rows returned)
# ══════════════════════════════════════════════════════════════

class AdminStatsResource(Resource):
    @admin_required
    def get(self):
        total    = Payment.query.count()
        paid     = Payment.query.filter(_pay_status == PaymentStatus.PAID).count()
        failed   = Payment.query.filter(_pay_status == PaymentStatus.FAILED).count()
        refunded = Payment.query.filter(_pay_status == PaymentStatus.REFUNDED).count()

        usd_revenue: float = (
            db.session.query(func.sum(cast(_pay_amount, Float)))
            .filter(_pay_status == PaymentStatus.PAID, _pay_currency == "USD")
            .scalar()
        ) or 0.0

        kes_revenue: float = (
            db.session.query(func.sum(cast(_pay_amount, Float)))
            .filter(_pay_status == PaymentStatus.PAID, _pay_currency == "KES")
            .scalar()
        ) or 0.0

        return {
            "payments": {
                "total":    total,
                "paid":     paid,
                "failed":   failed,
                "refunded": refunded,
            },
            "revenue": {
                "USD": round(usd_revenue, 2),
                "KES": round(kes_revenue, 2),
            },
            "sessions": {
                "total":     CheckoutSession.query.count(),
                "initiated": CheckoutSession.query.filter(_ses_status == SessionStatus.INITIATED).count(),
                "completed": CheckoutSession.query.filter(_ses_status == SessionStatus.COMPLETED).count(),
                "cancelled": CheckoutSession.query.filter(_ses_status == SessionStatus.CANCELLED).count(),
            },
            "invoices": {
                "total":     InvoiceRecord.query.count(),
                "orphaned":  InvoiceRecord.query.filter(_inv_session_id.is_(None)).count(),
                "abandoned": InvoiceRecord.query.filter(
                    _inv_session_id.isnot(None),
                    _inv_payment_id.is_(None),
                ).count(),
                "completed": InvoiceRecord.query.filter(
                    _inv_payment_id.isnot(None)
                ).count(),
            },
            "emails": {
                "sent":   EmailLog.query.filter(_email_status == EmailStatus.SENT).count(),
                "failed": EmailLog.query.filter(_email_status == EmailStatus.FAILED).count(),
            },
        }, 200


# ══════════════════════════════════════════════════════════════
#  PAYMENTS  (admin only)
# ══════════════════════════════════════════════════════════════

class AdminPaymentListResource(Resource):

    @admin_required
    def get(self):
        """
        Paginated, filtered, sorted payment list.

        Query params:
          page, per_page, status, currency, funding_source,
          search, date_from, date_to, sort_by, sort_dir
        """
        page     = int(request.args.get("page",     1))
        per_page = int(request.args.get("per_page", 25))

        q      = _build_payment_query()
        result = paginate(q, page, per_page)

        return {
            "data":       [p.to_dict() for p in result["items"]],
            "pagination": result["pagination"],
        }, 200


class AdminPaymentExportResource(Resource):

    @admin_required
    def get(self):
        """
        Export ALL payments matching current filters as JSON (no pagination).
        Same filter params as GET /api/admin/payments.
        """
        q        = _build_payment_query()
        payments = q.all()

        return {
            "count":       len(payments),
            "exported_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "data":        [p.to_dict() for p in payments],
        }, 200


class AdminPaymentBulkResource(Resource):

    @admin_required
    def post(self):
        """
        Bulk action on payments.

        Body:
          {
            "action":     "delete" | "status_update",
            "order_ids":  ["ABC123", ...],    // explicit list, OR
            "select_all": true,               // act on ALL matching filters
            "filters":    { ... },            // used only with select_all
            "status":     "FAILED"            // required for status_update
          }

        Limits:
          Explicit order_ids list : max 500 per call.
          select_all              : no limit (processed in batches of 500).
        """
        data       = request.get_json(force=True) or {}
        action     = (data.get("action") or "").lower()
        order_ids  = data.get("order_ids") or []
        select_all = bool(data.get("select_all", False))
        filters    = data.get("filters") or {}
        new_status = (data.get("status") or "").upper()

        # ── Validate action ───────────────────────────────────
        if action not in ("delete", "status_update"):
            return {
                "error":         "action must be 'delete' or 'status_update'",
                "valid_actions": ["delete", "status_update"],
            }, 400

        status_enum: Optional[PaymentStatus] = None

        if action == "status_update":
            try:
                status_enum = PaymentStatus(new_status)
            except ValueError:
                return {
                    "error":          f"Invalid status: {new_status}",
                    "valid_statuses": [s.value for s in PaymentStatus],
                }, 400

        # ── Build target query ────────────────────────────────
        if select_all:
            q = _build_payment_query_from_dict(filters)
        else:
            if not order_ids:
                return {"error": "Provide order_ids or set select_all=true"}, 400
            if len(order_ids) > 500:
                return {"error": "Maximum 500 order_ids per request"}, 400
            q = Payment.query.filter(_pay_order_id.in_(order_ids))

        # ── Execute in batches of 500 ─────────────────────────
        BATCH = 500
        total_affected = 0
        offset = 0

        while True:
            batch = q.offset(offset).limit(BATCH).all()
            if not batch:
                break

            for payment in batch:
                if action == "delete":
                    db.session.delete(payment)
                else:
                    payment.status = status_enum  # type: ignore[assignment]

            db.session.commit()
            total_affected += len(batch)
            offset += BATCH

            if len(batch) < BATCH:
                break

        verb = "deleted" if action == "delete" else f"updated to {new_status}"
        return {
            "message":        f"{total_affected} payment(s) {verb}",
            "affected_count": total_affected,
            "action":         action,
        }, 200


class AdminPaymentDetailResource(Resource):

    @admin_required
    def get(self, order_id: str):
        # Guard against reserved words that should never be treated as order_id
        if order_id in ("bulk", "export"):
            return {"error": f"'{order_id}' is not a valid order_id"}, 400

        p = Payment.query.filter_by(order_id=order_id).first()
        if not p:
            return {"error": "Payment not found"}, 404
        return p.to_dict(), 200

    @admin_required
    def patch(self, order_id: str):
        if order_id in ("bulk", "export"):
            return {"error": f"'{order_id}' is not a valid order_id"}, 400

        p = Payment.query.filter_by(order_id=order_id).first()
        if not p:
            return {"error": "Payment not found"}, 404

        data       = request.get_json(force=True) or {}
        new_status = (data.get("status") or "").upper()

        if new_status:
            try:
                p.status = PaymentStatus(new_status)
            except ValueError:
                return {
                    "error":          f"Invalid status: {new_status}",
                    "valid_statuses": [s.value for s in PaymentStatus],
                }, 400

        db.session.commit()
        return p.to_dict(), 200

    @admin_required
    def delete(self, order_id: str):
        if order_id in ("bulk", "export"):
            return {"error": f"'{order_id}' is not a valid order_id"}, 400

        p = Payment.query.filter_by(order_id=order_id).first()
        if not p:
            return {"error": "Payment not found"}, 404
        db.session.delete(p)
        db.session.commit()
        return {"message": f"Payment {order_id} deleted"}, 200


# ══════════════════════════════════════════════════════════════
#  SESSIONS  (admin only)
# ══════════════════════════════════════════════════════════════

class AdminSessionListResource(Resource):

    @admin_required
    def get(self):
        page     = int(request.args.get("page",     1))
        per_page = int(request.args.get("per_page", 25))
        status   = (request.args.get("status", "") or "").upper() or None

        q = CheckoutSession.query

        if status:
            try:
                q = q.filter(_ses_status == SessionStatus(status))
            except ValueError:
                pass

        q      = q.order_by(desc(_ses_created_at))
        result = paginate(q, page, per_page)

        return {
            "data":       [s.to_dict() for s in result["items"]],
            "pagination": result["pagination"],
        }, 200


class AdminSessionDetailResource(Resource):

    @admin_required
    def get(self, order_id: str):
        s = CheckoutSession.query.filter_by(order_id=order_id).first()
        if not s:
            return {"error": "Session not found"}, 404
        return s.to_dict(), 200


# ══════════════════════════════════════════════════════════════
#  INVOICE RECORDS  (admin only)
# ══════════════════════════════════════════════════════════════

class AdminInvoiceListResource(Resource):
    """
    GET  /api/admin/invoices
    ─────────────────────────────────────────────────────────
    Returns a paginated envelope:
      { "data": [...], "pagination": { ... } }

    Optional query params:
      ?filter=orphaned|abandoned|completed
      ?page=1&per_page=25   (per_page capped at 100 by paginate())

    The frontend fetches with per_page=1000 to load all records at once
    for client-side filtering/sorting; paginate() caps this at 100 per
    DB round-trip but the frontend treats the response as a flat list.

    NOTE: If you truly need all records in one shot without pagination,
    consider a dedicated export endpoint.  For now per_page=100 is the
    effective max per page — the frontend will only see 100 rows unless
    you raise the cap in paginate().
    """

    @admin_required
    def get(self):
        page     = int(request.args.get("page",     1))
        per_page = int(request.args.get("per_page", 25))

        # Optional state filter: orphaned / abandoned / completed
        inv_filter = (request.args.get("filter", "") or "").lower()

        q = InvoiceRecord.query

        if inv_filter == "orphaned":
            q = q.filter(_inv_session_id.is_(None))
        elif inv_filter == "abandoned":
            q = q.filter(
                _inv_session_id.isnot(None),
                _inv_payment_id.is_(None),
            )
        elif inv_filter == "completed":
            q = q.filter(_inv_payment_id.isnot(None))

        q      = q.order_by(desc(_inv_issued_at))
        result = paginate(q, page, per_page)

        return {
            "data":       [r.to_dict() for r in result["items"]],
            "pagination": result["pagination"],
        }, 200


class AdminInvoiceDetailResource(Resource):

    @admin_required
    def get(self, invoice_number: str):
        # Guard the reserved word used by cleanup route
        if invoice_number == "cleanup":
            return {"error": "'cleanup' is not a valid invoice_number"}, 400

        r = InvoiceRecord.query.filter_by(invoice_number=invoice_number).first()
        if not r:
            return {"error": "Invoice record not found"}, 404
        return r.to_dict(), 200


class AdminInvoiceCleanupResource(Resource):
    """
    POST /api/admin/invoices/cleanup
    ────────────────────────────────────────────────────────
    Deletes orphaned invoice records (no session_id AND no payment_id)
    that are older than `older_than_hours` hours (default: 24).

    Body (JSON, all optional):
      { "older_than_hours": 24 }

    Moved here from resources.py to consolidate all admin logic in one
    place and avoid the /api/admin/invoice/cleanup vs /api/admin/invoices
    path inconsistency.
    """

    @admin_required
    def post(self):
        data             = request.get_json(force=True) or {}
        older_than_hours = int(data.get("older_than_hours", 24))
        cutoff           = datetime.utcnow() - timedelta(hours=older_than_hours)

        orphans = (
            InvoiceRecord.query
            .filter(
                _inv_session_id.is_(None),
                _inv_payment_id.is_(None),
                _inv_issued_at < cutoff,
            )
            .all()
        )

        count = len(orphans)
        for record in orphans:
            db.session.delete(record)
        db.session.commit()

        return {
            "deleted":          count,
            "older_than_hours": older_than_hours,
            "cutoff":           cutoff.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "message":          f"Removed {count} orphaned invoice record(s).",
        }, 200


# ══════════════════════════════════════════════════════════════
#  EMAIL LOGS  (admin only)
# ══════════════════════════════════════════════════════════════

class AdminEmailLogListResource(Resource):

    @admin_required
    def get(self):
        page     = int(request.args.get("page",     1))
        per_page = int(request.args.get("per_page", 25))
        status   = (request.args.get("status", "") or "").upper() or None

        q = EmailLog.query

        if status:
            try:
                q = q.filter(_email_status == EmailStatus(status))
            except ValueError:
                pass

        q      = q.order_by(desc(_email_sent_at))
        result = paginate(q, page, per_page)

        return {
            "data":       [log.to_dict() for log in result["items"]],
            "pagination": result["pagination"],
        }, 200


class AdminEmailLogDetailResource(Resource):

    @admin_required
    def get(self, order_id: str):
        logs = EmailLog.query.filter(_email_order_id == order_id).all()
        if not logs:
            return {"error": "No email logs found for this order"}, 404
        return [log.to_dict() for log in logs], 200


# ══════════════════════════════════════════════════════════════
#  ADMIN PROFILE + ADMIN MANAGEMENT
# ══════════════════════════════════════════════════════════════

class AdminProfileResource(Resource):

    @admin_required
    def get(self):
        """Return the current admin's profile."""
        admin_id = get_jwt_identity()
        admin    = AdminUser.query.get(int(admin_id))
        if not admin:
            return {"error": "Admin not found"}, 404
        return {"admin": admin.to_dict()}, 200

    @admin_required
    def patch(self):
        """Update the current admin's full_name."""
        admin_id = get_jwt_identity()
        admin    = AdminUser.query.get(int(admin_id))
        if not admin:
            return {"error": "Admin not found"}, 404

        data      = request.get_json(force=True) or {}
        full_name = (data.get("full_name") or "").strip()

        if full_name:
            admin.full_name = full_name
            db.session.commit()

        return {"admin": admin.to_dict()}, 200


class AdminListResource(Resource):

    @admin_required
    def get(self):
        """List all admin accounts."""
        admins = AdminUser.query.order_by(AdminUser.created_at.asc()).all()
        return {"admins": [a.to_dict() for a in admins]}, 200

    @admin_required
    def post(self):
        """Create a new admin account (requires existing admin JWT)."""
        data      = request.get_json(force=True) or {}
        email     = (data.get("email")     or "").strip().lower()
        password  =  data.get("password")  or ""
        full_name = (data.get("full_name") or "").strip() or None

        if not email:
            return {"error": "email is required"}, 400
        if len(password) < 8:
            return {"error": "password must be at least 8 characters"}, 400
        if AdminUser.query.filter_by(email=email).first():
            return {"error": "An admin with that email already exists"}, 409

        new_admin = AdminUser(email=email, password=password, full_name=full_name)
        db.session.add(new_admin)
        db.session.commit()

        return {
            "message": "Admin account created",
            "admin":   new_admin.to_dict(),
        }, 201


# ══════════════════════════════════════════════════════════════
#  REGISTRATION HELPER
# ══════════════════════════════════════════════════════════════

def register_admin_resources(api) -> None:
    """
    Register all admin API resources.

    IMPORTANT — route ordering rules for Flask-RESTful:
    ────────────────────────────────────────────────────
    1. Static routes (no angle-bracket segments) MUST be registered
       BEFORE dynamic routes that share the same prefix.
       e.g. /admin/payments/bulk  must come before /admin/payments/<order_id>
            /admin/invoices/cleanup must come before /admin/invoices/<invoice_number>

    2. Using two separate Api() instances in app.py (public_api and
       admin_api) is intentional — it avoids blueprint-level prefix
       conflicts while keeping a clean /api prefix on both.
    """
    # ── Dashboard & stats ─────────────────────────────────────
    api.add_resource(AdminDashboardResource,      "/admin/dashboard")
    api.add_resource(AdminStatsResource,          "/admin/stats")

    # ── Payments — static routes first, then dynamic ──────────
    api.add_resource(AdminPaymentListResource,    "/admin/payments")
    api.add_resource(AdminPaymentExportResource,  "/admin/payments/export")   # static — before <order_id>
    api.add_resource(AdminPaymentBulkResource,    "/admin/payments/bulk")     # static — before <order_id>
    api.add_resource(AdminPaymentDetailResource,  "/admin/payments/<string:order_id>")

    # ── Sessions ──────────────────────────────────────────────
    api.add_resource(AdminSessionListResource,    "/admin/sessions")
    api.add_resource(AdminSessionDetailResource,  "/admin/sessions/<string:order_id>")

    # ── Invoices — static cleanup route first, then dynamic ───
    api.add_resource(AdminInvoiceListResource,    "/admin/invoices")
    api.add_resource(AdminInvoiceCleanupResource, "/admin/invoices/cleanup")  # static — before <invoice_number>
    api.add_resource(AdminInvoiceDetailResource,  "/admin/invoices/<string:invoice_number>")

    # ── Email logs ────────────────────────────────────────────
    api.add_resource(AdminEmailLogListResource,   "/admin/email-logs")
    api.add_resource(AdminEmailLogDetailResource, "/admin/email-logs/<string:order_id>")

    # ── Admin profile + management ────────────────────────────
    api.add_resource(AdminProfileResource,        "/admin/profile")
    api.add_resource(AdminListResource,           "/admin/admins")
"""
models.py
─────────────────────────────────────────────────────────────
SQLAlchemy models for the PayPal payment application.

Tables & relationships
──────────────────────
  admin_users        — admin accounts for dashboard access (JWT auth)
  invoice_counters   — per-year atomic sequence counter (one row per year)
  invoice_records    — one row per issued invoice number; the single source
                       of truth that ties a generated invoice to its session
                       and eventual payment
  checkout_sessions  — every PayPal order created (before capture)
  payments           — every captured/completed PayPal payment
  email_logs         — every confirmation email attempt

Relationship map
────────────────
  InvoiceRecord  1 ──── 1   CheckoutSession   (invoice_records.session_id → checkout_sessions.id)
  InvoiceRecord  1 ──── 0/1 Payment           (invoice_records.payment_id → payments.id)
  Payment        1 ──── *   EmailLog          (email_logs.order_id → payments.order_id)

  InvoiceCounter is a standalone counter table — it has no FK relationships
  because it only holds the rolling sequence number, not per-invoice data.

  AdminUser is a standalone table — it has no FK relationships to the
  payment tables.  It exists solely to authenticate dashboard access.
"""

import enum
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()
migrate = Migrate()


# ══════════════════════════════════════════════════════════════
#  ENUMS
# ══════════════════════════════════════════════════════════════
class SessionStatus(enum.Enum):
    INITIATED = "INITIATED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED    = "FAILED"

    def __str__(self):
        return self.value


class PaymentStatus(enum.Enum):
    PAID     = "PAID"
    REFUNDED = "REFUNDED"
    FAILED   = "FAILED"

    def __str__(self):
        return self.value


class EmailStatus(enum.Enum):
    SENT    = "SENT"
    FAILED  = "FAILED"
    SKIPPED = "SKIPPED"

    def __str__(self):
        return self.value


class FundingSource(enum.Enum):
    PAYPAL = "paypal"
    CARD   = "card"

    def __str__(self):
        return self.value


# ══════════════════════════════════════════════════════════════
#  0a. ADMIN USER
#  ─────────────────────────────────────────────────────────────
#  Standalone admin accounts table.  This has NO relationship to
#  the payment tables — it exists only to authenticate dashboard
#  access via JWT.
#
#  Only one admin should exist in most deployments.  A second
#  admin can be created via POST /api/auth/admin/register using
#  an existing admin's JWT token.
#
#  Password is always stored as a bcrypt hash via werkzeug.
# ══════════════════════════════════════════════════════════════
class AdminUser(db.Model):
    __tablename__ = "admin_users"

    id         = db.Column(db.Integer,     primary_key=True, autoincrement=True)
    email      = db.Column(db.String(150), nullable=False, unique=True, index=True)
    password   = db.Column(db.String(255), nullable=False)
    full_name  = db.Column(db.String(100), nullable=True)
    is_active  = db.Column(db.Boolean,     nullable=False, default=True)
    created_at = db.Column(db.DateTime,    nullable=False, default=datetime.utcnow)
    last_login = db.Column(db.DateTime,    nullable=True)

    def __init__(self, email: str, password: str, full_name: str | None = None):
        self.email     = email.lower().strip()
        self.full_name = full_name
        self.set_password(password)

    # ── password helpers ──────────────────────────────────────
    def set_password(self, raw_password: str) -> None:
        """Hash and store the password."""
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        """Return True if raw_password matches the stored hash."""
        return check_password_hash(self.password, raw_password)

    def record_login(self) -> None:
        """Stamp last_login with the current UTC time."""
        self.last_login = datetime.utcnow()

    def to_dict(self):
        return {
            "id":         self.id,
            "email":      self.email,
            "full_name":  self.full_name,
            "is_active":  self.is_active,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "last_login": self.last_login.strftime("%Y-%m-%d %H:%M:%S") if self.last_login else None,
        }

    def __repr__(self):
        return f"<AdminUser {self.email} | active={self.is_active}>"


# ══════════════════════════════════════════════════════════════
#  0b. INVOICE COUNTER
#  ─────────────────────────────────────────────────────────────
#  Standalone counter table — one row per calendar year.
#  `last_seq` is incremented atomically (SELECT … FOR UPDATE)
#  each time GET /api/invoice/generate is called.
#
#  This table intentionally has NO foreign keys — it is purely
#  a sequence generator, not a record of invoices.  The actual
#  per-invoice record is stored in InvoiceRecord below.
#
#  Example:  year=2026, last_seq=42
#    → the next InvoiceRecord will carry INV-2026-00043-<HMAC>
# ══════════════════════════════════════════════════════════════
class InvoiceCounter(db.Model):
    __tablename__ = "invoice_counters"

    id       = db.Column(db.Integer, primary_key=True, autoincrement=True)
    year     = db.Column(db.Integer, nullable=False, unique=True, index=True)
    last_seq = db.Column(db.Integer, nullable=False, default=0)

    def __init__(self, year: int, last_seq: int = 0):
        self.year     = year
        self.last_seq = last_seq

    def __repr__(self):
        return f"<InvoiceCounter year={self.year} last_seq={self.last_seq}>"


# ══════════════════════════════════════════════════════════════
#  1.  INVOICE RECORDS
#  ─────────────────────────────────────────────────────────────
#  Created immediately when the frontend calls
#  GET /api/invoice/generate.  At that point only `invoice_number`,
#  `year`, and `sequence` are known; both FK columns are NULL.
#
#  FK wiring (both set later in the payment flow):
#    session_id  → checkout_sessions.id   (set when /api/pay is called)
#    payment_id  → payments.id            (set when /success capture runs)
#
#  Back-references exposed on the related models:
#    CheckoutSession.invoice_record  → the one InvoiceRecord (or None)
#    Payment.invoice_record          → the one InvoiceRecord (or None)
# ══════════════════════════════════════════════════════════════
class InvoiceRecord(db.Model):
    __tablename__ = "invoice_records"

    id             = db.Column(db.Integer,    primary_key=True, autoincrement=True)
    invoice_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    year           = db.Column(db.Integer,    nullable=False, index=True)
    sequence       = db.Column(db.Integer,    nullable=False)

    session_id = db.Column(
        db.Integer,
        db.ForeignKey("checkout_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    payment_id = db.Column(
        db.Integer,
        db.ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    issued_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    session = db.relationship(
        "CheckoutSession",
        back_populates="invoice_record",
        foreign_keys=[session_id],
    )
    payment = db.relationship(
        "Payment",
        back_populates="invoice_record",
        foreign_keys=[payment_id],
    )

    def __init__(
        self,
        invoice_number: str,
        year:           int,
        sequence:       int,
        session_id:     int | None = None,
        payment_id:     int | None = None,
    ):
        self.invoice_number = invoice_number
        self.year           = year
        self.sequence       = sequence
        self.session_id     = session_id
        self.payment_id     = payment_id

    def to_dict(self):
        return {
            "id":             self.id,
            "invoice_number": self.invoice_number,
            "year":           self.year,
            "sequence":       self.sequence,
            "session_id":     self.session_id,
            "payment_id":     self.payment_id,
            "issued_at":      self.issued_at.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def __repr__(self):
        return (
            f"<InvoiceRecord {self.invoice_number} "
            f"| session={self.session_id} | payment={self.payment_id}>"
        )


# ══════════════════════════════════════════════════════════════
#  2.  CHECKOUT SESSIONS
# ══════════════════════════════════════════════════════════════
class CheckoutSession(db.Model):
    __tablename__ = "checkout_sessions"

    id             = db.Column(db.Integer,             primary_key=True, autoincrement=True)
    order_id       = db.Column(db.String(100),         nullable=False, unique=True, index=True)
    invoice_number = db.Column(db.String(100),         nullable=True,  index=True)
    amount         = db.Column(db.String(20),          nullable=False)
    currency       = db.Column(db.String(10),          nullable=False, default="USD")
    description    = db.Column(db.String(255),         nullable=True)
    funding_source = db.Column(db.Enum(FundingSource), nullable=False, default=FundingSource.PAYPAL)
    status         = db.Column(db.Enum(SessionStatus), nullable=False, default=SessionStatus.INITIATED, index=True)
    created_at     = db.Column(db.DateTime,            nullable=False, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime,            nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    invoice_record = db.relationship(
        "InvoiceRecord",
        back_populates="session",
        uselist=False,
        foreign_keys="InvoiceRecord.session_id",
    )

    def __init__(
        self,
        order_id:       str,
        amount:         str,
        currency:       str           = "USD",
        description:    str | None    = None,
        invoice_number: str | None    = None,
        funding_source: FundingSource = FundingSource.PAYPAL,
        status:         SessionStatus = SessionStatus.INITIATED,
    ):
        self.order_id       = order_id
        self.invoice_number = invoice_number
        self.amount         = amount
        self.currency       = currency
        self.description    = description
        self.funding_source = funding_source
        self.status         = status

    def to_dict(self):
        return {
            "id":                self.id,
            "order_id":          self.order_id,
            "invoice_number":    self.invoice_number,
            "amount":            self.amount,
            "currency":          self.currency,
            "description":       self.description,
            "funding_source":    self.funding_source.value if self.funding_source else None,
            "status":            self.status.value if self.status else None,
            "created_at":        self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at":        self.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_record_id": self.invoice_record.id if self.invoice_record else None,
        }

    def __repr__(self):
        return f"<CheckoutSession {self.order_id} | inv={self.invoice_number} | {self.status}>"


# ══════════════════════════════════════════════════════════════
#  3.  PAYMENTS
# ══════════════════════════════════════════════════════════════
class Payment(db.Model):
    __tablename__ = "payments"

    id             = db.Column(db.Integer,             primary_key=True, autoincrement=True)
    order_id       = db.Column(db.String(100),         nullable=False, unique=True, index=True)
    invoice_number = db.Column(db.String(100),         nullable=True,  index=True)
    amount         = db.Column(db.String(20),          nullable=False)
    currency       = db.Column(db.String(10),          nullable=False, default="USD")
    description    = db.Column(db.String(255),         nullable=True)
    status         = db.Column(db.Enum(PaymentStatus), nullable=False, default=PaymentStatus.PAID, index=True)
    payer_email    = db.Column(db.String(150),         nullable=True)
    payer_name     = db.Column(db.String(100),         nullable=True)
    payer_id       = db.Column(db.String(100),         nullable=True)
    capture_id     = db.Column(db.String(100),         nullable=True)
    funding_source = db.Column(db.Enum(FundingSource), nullable=True)
    created_at     = db.Column(db.DateTime,            nullable=False, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime,            nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    invoice_record = db.relationship(
        "InvoiceRecord",
        back_populates="payment",
        uselist=False,
        foreign_keys="InvoiceRecord.payment_id",
    )

    email_logs = db.relationship(
        "EmailLog",
        backref="payment",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="EmailLog.order_id",
    )

    def __init__(
        self,
        order_id:       str,
        amount:         str,
        currency:       str                  = "USD",
        description:    str | None           = None,
        invoice_number: str | None           = None,
        status:         PaymentStatus        = PaymentStatus.PAID,
        payer_email:    str | None           = None,
        payer_name:     str | None           = None,
        payer_id:       str | None           = None,
        capture_id:     str | None           = None,
        funding_source: FundingSource | None = None,
    ):
        self.order_id       = order_id
        self.invoice_number = invoice_number
        self.amount         = amount
        self.currency       = currency
        self.description    = description
        self.status         = status
        self.payer_email    = payer_email
        self.payer_name     = payer_name
        self.payer_id       = payer_id
        self.capture_id     = capture_id
        self.funding_source = funding_source

    def to_dict(self):
        return {
            "id":                self.id,
            "order_id":          self.order_id,
            "invoice_number":    self.invoice_number,
            "amount":            self.amount,
            "currency":          self.currency,
            "description":       self.description,
            "status":            self.status.value if self.status else None,
            "payer_email":       self.payer_email,
            "payer_name":        self.payer_name,
            "payer_id":          self.payer_id,
            "capture_id":        self.capture_id,
            "funding_source":    self.funding_source.value if self.funding_source else None,
            "created_at":        self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at":        self.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_record_id": self.invoice_record.id if self.invoice_record else None,
        }

    def __repr__(self):
        return (
            f"<Payment {self.order_id} | inv={self.invoice_number} "
            f"| {self.currency} {self.amount} | {self.status}>"
        )


# ══════════════════════════════════════════════════════════════
#  4.  EMAIL LOGS
# ══════════════════════════════════════════════════════════════
class EmailLog(db.Model):
    __tablename__ = "email_logs"

    id       = db.Column(db.Integer,           primary_key=True, autoincrement=True)
    order_id = db.Column(
        db.String(100),
        db.ForeignKey("payments.order_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_email = db.Column(db.String(150),       nullable=False)
    subject  = db.Column(db.String(255),       nullable=True)
    status   = db.Column(db.Enum(EmailStatus), nullable=False, default=EmailStatus.SENT)
    error    = db.Column(db.Text,              nullable=True)
    sent_at  = db.Column(db.DateTime,          nullable=False, default=datetime.utcnow)

    def __init__(
        self,
        order_id: str,
        to_email: str,
        subject:  str | None  = None,
        status:   EmailStatus = EmailStatus.SENT,
        error:    str | None  = None,
    ):
        self.order_id = order_id
        self.to_email = to_email
        self.subject  = subject
        self.status   = status
        self.error    = error

    def to_dict(self):
        return {
            "id":       self.id,
            "order_id": self.order_id,
            "to_email": self.to_email,
            "subject":  self.subject,
            "status":   self.status.value if self.status else None,
            "error":    self.error,
            "sent_at":  self.sent_at.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def __repr__(self):
        return f"<EmailLog {self.order_id} → {self.to_email} | {self.status}>"
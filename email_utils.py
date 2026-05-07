"""
email_utils.py
─────────────────────────────────────────────────────────────
Email sending helper for the PayPal payment application.

All configuration is sourced from config.Config — no direct
os.getenv() calls here.
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import Config
from models import db, EmailLog, EmailStatus


def send_email(to_email: str, order_id: str, amount: str, currency: str) -> str:
    """
    Send a payment confirmation email.
    Returns 'SENT' or 'FAILED'. Always logs — never raises.
    """
    subject = "Payment Confirmation — Invoice Paid"
    body = (
        f"Thank you! Your payment was successful.\n\n"
        f"Order ID : {order_id}\n"
        f"Amount   : {currency} {amount}\n\n"
        f"Please keep this for your records.\n"
    )

    msg            = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = Config.SMTP_EMAIL
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain"))

    error_msg: str | None = None
    status                = EmailStatus.SENT

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(Config.SMTP_EMAIL, Config.SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as exc:
        status    = EmailStatus.FAILED
        error_msg = str(exc)
        print(f"[Email] Failed to send to {to_email}: {exc}")

    log = EmailLog(
        order_id=order_id,
        to_email=to_email,
        subject=subject,
        status=status,
        error=error_msg,
    )
    db.session.add(log)
    db.session.commit()

    return status.value
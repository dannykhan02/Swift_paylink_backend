"""
paypal.py
─────────────────────────────────────────────────────────────
PayPal API helpers: access token, create order, capture order.

All configuration is sourced from config.Config — no direct
os.getenv() calls here.
"""

import base64
import requests as http

from config import Config


def get_access_token() -> str:
    credentials = base64.b64encode(
        f"{Config.PAYPAL_CLIENT_ID}:{Config.PAYPAL_CLIENT_SECRET}".encode()
    ).decode()

    res = http.post(
        f"{Config.PAYPAL_BASE_URL}/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials",
        timeout=15,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def create_paypal_order(
    token:          str,
    amount:         str,
    currency:       str,
    description:    str,
    funding_source: str = "paypal",
) -> dict:
    """
    funding_source = 'paypal' → PayPal login landing page
    funding_source = 'card'   → Direct card entry landing page
    """
    landing_page = "BILLING" if funding_source == "card" else "LOGIN"

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {
                "currency_code": currency,
                "value":         amount,
            },
            "description": description,
        }],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                    "brand_name":   "PayPal Invoice",
                    "locale":       "en-US",
                    "landing_page": landing_page,
                    "user_action":  "PAY_NOW",
                    "return_url":   Config.RETURN_URL,
                    "cancel_url":   Config.CANCEL_URL,
                }
            }
        },
    }

    res = http.post(
        f"{Config.PAYPAL_BASE_URL}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=15,
    )
    res.raise_for_status()
    return res.json()


def capture_paypal_order(token: str, order_id: str) -> dict:
    res = http.post(
        f"{Config.PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        timeout=15,
    )
    res.raise_for_status()
    return res.json()
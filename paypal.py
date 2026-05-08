"""
paypal.py
─────────────────────────────────────────────────────────────
PayPal API helpers: access token, create order, capture order.

All configuration is sourced from config.Config — no direct
os.getenv() calls here.
"""

import base64
import uuid

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

    if not res.ok:
        print(f"[PayPal] get_access_token failed {res.status_code}: {res.text}")
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
                    "brand_name":   "Swift Pay Link",
                    "locale":       "en-US",
                    "landing_page": landing_page,
                    "user_action":  "PAY_NOW",
                    "return_url":   Config.RETURN_URL,
                    "cancel_url":   Config.CANCEL_URL,
                }
            }
        },
    }

    print(f"[PayPal] Creating order — amount: {amount} {currency}, mode: {'sandbox' if Config.IS_SANDBOX else 'live'}")

    res = http.post(
        f"{Config.PAYPAL_BASE_URL}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=15,
    )

    if not res.ok:
        print(f"[PayPal] create_paypal_order failed {res.status_code}: {res.text}")
        res.raise_for_status()

    order = res.json()
    print(f"[PayPal] Order created: {order.get('id')} status={order.get('status')}")
    return order


def get_order_status(token: str, order_id: str) -> str:
    """
    Returns the PayPal order status string, e.g. 'CREATED', 'APPROVED',
    'COMPLETED', 'VOIDED', 'PAYER_ACTION_REQUIRED'.
    Raises on HTTP error.
    """
    res = http.get(
        f"{Config.PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )

    if not res.ok:
        print(f"[PayPal] get_order_status failed {res.status_code}: {res.text}")
        res.raise_for_status()

    status = res.json().get("status", "UNKNOWN")
    print(f"[PayPal] Order {order_id} status: {status}")
    return status


def capture_paypal_order(token: str, order_id: str) -> dict:
    """
    Capture an approved PayPal order.

    Guard against double-capture (which causes a 422):
      1. Fetch the order status first.
      2. If it is already COMPLETED, return a synthetic dict so the
         caller can proceed without hitting PayPal again.
      3. If it is not APPROVED, raise so the caller knows it cannot
         be captured yet.
      4. Use PayPal-Request-Id for idempotency — safe to retry on
         transient network errors without double-charging.
    """
    status = get_order_status(token, order_id)

    if status == "COMPLETED":
        print(f"[PayPal] Order {order_id} already COMPLETED — skipping capture.")
        return {"status": "COMPLETED", "id": order_id, "_already_captured": True}

    if status != "APPROVED":
        raise ValueError(
            f"[PayPal] Cannot capture order {order_id}: status is '{status}' (expected APPROVED)"
        )

    idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"capture:{order_id}"))
    print(f"[PayPal] Capturing order {order_id} with idempotency key {idempotency_key}")

    res = http.post(
        f"{Config.PAYPAL_BASE_URL}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization":     f"Bearer {token}",
            "Content-Type":      "application/json",
            "PayPal-Request-Id": idempotency_key,
        },
        timeout=15,
    )

    if not res.ok:
        print(f"[PayPal] capture_paypal_order failed {res.status_code}: {res.text}")
        res.raise_for_status()

    result = res.json()
    print(f"[PayPal] Capture result: {result.get('status')} for order {order_id}")
    return result
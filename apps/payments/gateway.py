"""
Razorpay integration through its official Python SDK. Secrets stay on the
server; the browser only receives the public key ID and the gateway order ID.

Signatures:
  * Checkout: HMAC-SHA256 of "<order_id>|<payment_id>" with the key secret.
  * Webhooks: HMAC-SHA256 of the raw request body with the webhook secret,
    sent in the X-Razorpay-Signature header.
Both are verified with the SDK's constant-time helpers.
"""

import logging

import razorpay
import requests
from django.conf import settings
from razorpay.errors import BadRequestError, GatewayError, ServerError, SignatureVerificationError
from rest_framework import status
from rest_framework.exceptions import APIException

logger = logging.getLogger("prepwise.payments")

RAZORPAY = "razorpay"


class PaymentsUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "payments_unavailable"
    default_detail = "Online payment isn't available right now. Please try again later."


class PaymentGatewayError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "payment_gateway_unavailable"
    default_detail = "We couldn't reach the payment provider. Please try again in a moment."


class RazorpayGateway:
    name = RAZORPAY

    def __init__(self, *, key_id, key_secret, webhook_secret, timeout, session=None):
        self.key_id = key_id
        self._webhook_secret = webhook_secret
        self.timeout = timeout
        self.client = razorpay.Client(auth=(key_id, key_secret), session=session)

    def _call(self, func, *args):
        try:
            return func(*args, timeout=self.timeout)
        except (BadRequestError, GatewayError, ServerError, requests.RequestException) as exc:
            # The exception class is enough to diagnose; messages may echo request data.
            logger.error("Razorpay request failed: %s", type(exc).__name__)
            raise PaymentGatewayError() from exc

    def create_order(self, *, amount_minor, currency, receipt, notes):
        return self._call(
            self.client.order.create,
            {"amount": amount_minor, "currency": currency, "receipt": receipt, "notes": notes},
        )

    def fetch_payment(self, payment_id):
        return self._call(self.client.payment.fetch, payment_id)

    def refund(self, payment_id, *, amount_minor, notes):
        return self._call(
            self.client.payment.refund, payment_id, {"amount": amount_minor, "notes": notes}
        )

    def verify_payment_signature(self, *, order_id, payment_id, signature):
        if not (order_id and payment_id and signature):
            return False
        try:
            return bool(
                self.client.utility.verify_payment_signature(
                    {
                        "razorpay_order_id": order_id,
                        "razorpay_payment_id": payment_id,
                        "razorpay_signature": signature,
                    }
                )
            )
        except SignatureVerificationError:
            return False

    def verify_webhook_signature(self, body, signature):
        if not signature or not self._webhook_secret:
            return False
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        try:
            return bool(
                self.client.utility.verify_webhook_signature(text, signature, self._webhook_secret)
            )
        except SignatureVerificationError:
            return False


def is_configured():
    config = settings.PAYMENT_SETTINGS
    return config["PROVIDER"] == RAZORPAY and all(
        config[name] for name in ("KEY_ID", "KEY_SECRET", "WEBHOOK_SECRET")
    )


def get_gateway():
    """The configured gateway (tests replace this). Raises PaymentsUnavailable."""
    if not is_configured():
        raise PaymentsUnavailable()
    config = settings.PAYMENT_SETTINGS
    return RazorpayGateway(
        key_id=config["KEY_ID"],
        key_secret=config["KEY_SECRET"],
        webhook_secret=config["WEBHOOK_SECRET"],
        timeout=config["REQUEST_TIMEOUT_SECONDS"],
    )

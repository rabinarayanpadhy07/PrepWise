"""
Payment races across separate database connections: the checkout handler and
webhooks arriving together, and refunds requested twice at once.
"""

import threading
import time
import uuid

import pytest
from django.db import connection

from apps.billing.models import Subscription
from apps.payments import gateway as gateway_module
from apps.payments import services
from apps.payments.models import Invoice, Payment, PaymentOrder, Refund
from apps.payments.tests.test_payments import (
    FakeRazorpay,
    create_order,
    payment_entity,
    send_webhook,
    verify,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("seeded_transactional_db"),
]


class SlowRazorpay(FakeRazorpay):
    """Holds each refund open briefly so concurrent requests overlap."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def refund(self, payment_id, *, amount_minor, notes):
        time.sleep(0.2)
        refund = {
            "id": f"rfnd_{uuid.uuid4().hex[:14]}",
            "payment_id": payment_id,
            "amount": amount_minor,
            "currency": "INR",
            "status": "processed",
        }
        with self._lock:
            self.refunds.append(refund)
        return refund


@pytest.fixture
def gateway(monkeypatch):
    fake = SlowRazorpay()
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: fake)
    return fake


def race(actions):
    """Run each callable in its own thread at the same moment; return results or exceptions."""
    barrier = threading.Barrier(len(actions))
    results = [None] * len(actions)

    def target(index):
        try:
            barrier.wait()
            results[index] = actions[index]()
        except Exception as exc:  # noqa: BLE001 - reported to the test
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=target, args=(i,)) for i in range(len(actions))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


def test_checkout_and_webhooks_racing_activate_one_subscription(
    user, client_for, api_client, gateway
):
    client = client_for(user)
    order = PaymentOrder.objects.get(pk=create_order(client).json()["id"])
    entity = payment_entity(order)
    gateway.payments[entity["id"]] = entity

    results = race(
        [
            lambda: verify(client_for(user), order).status_code,
            lambda: verify(client_for(user), order).status_code,
            lambda: (
                send_webhook(
                    api_client.__class__(), "payment.captured", {"payment": entity}
                ).status_code
            ),
            lambda: (
                send_webhook(
                    api_client.__class__(), "order.paid", {"payment": entity}, event_id="evt_2"
                ).status_code
            ),
        ]
    )

    assert results == [200, 200, 200, 200]
    order.refresh_from_db()
    assert order.status == "paid"
    assert Subscription.objects.filter(user=user).count() == 1
    assert Payment.objects.count() == 1
    assert Invoice.objects.count() == 1


def test_concurrent_full_refunds_refund_once(user, client_for, gateway):
    client = client_for(user)
    order = PaymentOrder.objects.get(pk=create_order(client).json()["id"])
    gateway.payments["pay_test0001"] = payment_entity(order)
    assert verify(client, order).status_code == 200

    def refund():
        payment = Payment.objects.get(gateway_payment_id="pay_test0001")
        return services.request_refund(payment, reason="Duplicate click")

    results = race([refund, refund])

    refused = [r for r in results if isinstance(r, ValueError)]
    assert len(gateway.refunds) == 1
    assert len(refused) == 1
    assert Refund.objects.count() == 1
    payment = Payment.objects.get(gateway_payment_id="pay_test0001")
    assert payment.amount_refunded_minor == payment.amount_minor


def test_concurrent_partial_refunds_never_exceed_the_payment(user, client_for, gateway):
    client = client_for(user)
    order = PaymentOrder.objects.get(pk=create_order(client).json()["id"])
    gateway.payments["pay_test0001"] = payment_entity(order)
    assert verify(client, order).status_code == 200
    share = order.amount_minor * 2 // 3

    def refund():
        payment = Payment.objects.get(gateway_payment_id="pay_test0001")
        return services.request_refund(payment, amount_minor=share, reason="Partial")

    race([refund, refund])

    assert sum(r["amount"] for r in gateway.refunds) <= order.amount_minor
    assert len(gateway.refunds) == 1

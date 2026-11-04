# Subscriptions and payments

## Plan and terms

One plan is seeded: **Platform AI · 30 days**, ₹5,000, a one-time payment with **manual renewal
only** and no automatic recurring billing. Administrators change the price, duration or allowances
in the admin panel (**Plans**, `/admin/plans`). Each change creates a new immutable `PlanVersion`. Orders
and subscriptions keep the version they were bought under, so existing purchases never change.

## Gateway: Razorpay

Integrated through the official `razorpay` Python SDK (server side) and Razorpay Checkout (browser).
PrepWise never handles card or UPI details.

1. **Create order** (`POST /api/v1/payments/orders/` with only `plan_code`). The amount and
   currency come from the plan's current version; anything else the browser sends is ignored. The
   backend creates the Razorpay order and checks that it echoes the same amount and currency.
2. **Checkout**: the browser opens Razorpay Checkout with the public key ID and the gateway order ID.
3. **Verify** (`POST /api/v1/payments/orders/<id>/verify/`): the backend checks the Checkout
   signature (HMAC-SHA256 of `order_id|payment_id` with the key secret), then **fetches the payment
   from Razorpay**. It applies the payment only if the payment belongs to this order and is
   `captured`, with the exact amount and currency.
4. **Webhooks** (`POST /api/v1/payments/webhooks/razorpay/`): verified against the raw body with the
   webhook secret (`X-Razorpay-Signature`). Deliveries are deduplicated by `X-Razorpay-Event-Id`,
   so repeats are no-ops. Processing errors return 500 so Razorpay retries.

A subscription is activated **only** for a verified, captured, matching payment. Activation is
idempotent: Checkout verification and any number of webhook deliveries create one subscription and
one invoice. A captured payment that doesn't match its order puts the order in `review` and grants
nothing. A second captured payment for an already-paid order is flagged `is_duplicate` for refunding.
The UI shows success only after the backend reports the order as `paid`. Otherwise it keeps polling,
because the webhook may confirm the payment shortly afterwards.

**Manual renewal:** a purchase made while a period is active (or already queued) starts when the
last one ends. After expiry, platform AI stops until the user buys again.

## Refunds and reversals

| Event | Effect on access |
| --- | --- |
| Full refund processed (`refund.processed` reaching the payment amount) | The subscription bought by that payment is **revoked immediately** |
| Partial refund | Recorded; access is kept |
| Refund pending or failed | Recorded; access is kept |
| Dispute (chargeback) opened | Recorded; access is kept while it is open |
| Dispute lost (`payment.dispute.lost`) | Subscription **revoked immediately** |
| Dispute won or closed | Recorded; nothing else changes |

Administrators refund through the gateway with
`python manage.py refund_payment pay_XXXX [--amount <paise>] [--reason "..."]`. Without `--amount`,
the remaining amount is refunded, which revokes the subscription.

## Invoices

An invoice is issued once, when a verified payment activates a subscription. It uses a sequential
number per year (`INVOICE_NUMBER_PREFIX-YYYY-000001`), and its details are snapshotted so it never
changes. Users download PDFs from **Billing** (`GET /api/v1/payments/invoices/<id>/pdf/`, owner only).
**PrepWise does not calculate or itemise tax.** Set `INVOICE_SELLER_*` (and a tax ID only if you are
registered), and confirm invoice requirements with your accountant before charging customers.

## Configuration

| Variable | Notes |
| --- | --- |
| `PAYMENT_PROVIDER` | `razorpay`, or empty to disable purchases (admins can still grant subscriptions) |
| `PAYMENT_KEY_ID` | Publishable key ID; sent to Checkout |
| `PAYMENT_KEY_SECRET` | Server only |
| `PAYMENT_WEBHOOK_SECRET` | The secret entered when creating the webhook; server only |
| `PAYMENT_REQUEST_TIMEOUT_SECONDS` | Gateway API timeout |
| `INVOICE_SELLER_NAME`, `_ADDRESS`, `_EMAIL`, `_TAX_ID`, `INVOICE_NUMBER_PREFIX` | Printed on invoices |

Get the keys from your own Razorpay dashboard: test-mode keys for development, live keys for
production. None are included in this repository. In the dashboard, add a webhook pointing at
`https://<api host>/api/v1/payments/webhooks/razorpay/` with the events `payment.authorized`,
`payment.captured`, `payment.failed`, `order.paid`, `refund.created`, `refund.processed`,
`refund.failed` and the `payment.dispute.*` events. Production refuses to start if
`PAYMENT_PROVIDER` is set without all three secrets.

## Tests

`apps/payments/tests/test_payments.py` uses the real `RazorpayGateway` class with real HMAC
signature checks and test-only secrets. Only its network calls (create order, fetch payment, refund)
are replaced. It covers success, failure, invalid signatures, duplicate webhooks, expiry, refunds,
disputes, manual renewal and plan-version snapshots. The integration has not been run against
Razorpay's live or test API in this repository's checks.

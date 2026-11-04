# PrepWise backend

Django REST Framework API on PostgreSQL. See the [root README](../README.md) for full Windows setup,
including creating the database.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # then set DB_PASSWORD and DJANGO_SECRET_KEY
python manage.py migrate
python manage.py runserver
pytest
```

## Structure

```
config/
├── settings/      base, development, production, test
├── urls.py        /admin/ and /api/<version>/ (only v1 allowed), JSON error handlers
└── api_v1.py      v1 routes
common/            Error envelope, request IDs, secret-redacting logging, health views, PDF helper
apps/
├── accounts/      Email-based User model, JWT auth, roles
├── administration/User management for administrators
├── ai_services/   OpenAI adapter, AI modes, routing and fallback, the admin-managed
│                  OpenAI key pool with rotation (AES-256-GCM), access status,
│                  basic (rule-based) fallback helpers
├── usage/         Usage ledger, quota reservations, limits, spending cap, idempotency keys
├── billing/       Plans, immutable plan versions, subscriptions
├── payments/      Razorpay orders, verification, webhooks, refunds, invoices
├── resumes/       Resume upload and extraction, job descriptions, AI analysis
├── interviews/    Mock interviews (text and voice), AI evaluation, progress summaries
└── dashboard/     Dashboard summary, activity feed, PDF reports
docs/              ai.md, usage-limits.md, payments.md, administration.md
requirements/      base.txt, dev.txt, prod.txt
```

## Management commands

| Command | Purpose |
| --- | --- |
| `generate_encryption_key [--id ID]` | Print a new `AI_KEY_ENCRYPTION_KEYS` entry (nothing is saved) |
| `rotate_encryption_keys [--dry-run]` | Re-encrypt the saved OpenAI keys with the active encryption key |
| `expire_usage_reservations` | Mark abandoned quota reservations as expired (safe on a schedule) |
| `refund_payment PAYMENT_ID [--amount PAISE] [--reason TEXT]` | Refund through the gateway |

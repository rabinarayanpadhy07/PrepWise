# Administration

Administrators manage PrepWise from the admin panel at `/admin` (frontend), backed by the API under
`/api/v1/admin/`. It is separate from the user app: it has its own sign-in page at `/admin/login`
(email and password, or Google), which calls `/api/v1/auth/admin/login/` or
`/api/v1/auth/admin/google/`. Those endpoints only start a session for users with `role = "admin"`;
anyone else gets `403 admin_required` and no tokens. Admin Google sign-in never creates an account.
Old `/app/admin/...` links redirect to `/admin/...`.

There is no Django admin site. Create the first administrator with either:

- `python manage.py createsuperuser` (new account with a password), or
- `python manage.py promote_admin person@example.com` (existing account, including one created with
  Google sign-in). The promotion is written to the audit log.

## Access control

- Every `/api/v1/admin/` endpoint requires an authenticated, active administrator
  (`IsAuthenticated` + `IsAdminRole`). Anonymous requests get `401`; normal users get `403`.
  `apps/administration/tests/test_admin_api.py` checks this for every admin route, and
  `common/tests/test_route_access.py` checks that every non-public API route rejects anonymous
  requests.
- The frontend hides the dashboard from normal users and makes no admin API calls for them, but
  the server is the boundary.
- Administrators can't change their own role, suspend themselves, or revoke their own sessions.

## What is never shown

- OpenAI API keys: only the masked form (`••••abcd`, the last four characters), status and usage.
  Plaintext keys and ciphertext never leave the database, and the audit log records only the last
  four characters.
- Gateway secrets and encryption keys: never shown.
- Interview questions, answers, transcripts, resume text and AI feedback: the user summary shows
  counts and dates only.
- Webhook payloads (they contain customer details): only event type, status and attempts.
- Provider call log: outcome codes, model, feature, latency and whether a fallback was used. No
  prompts, responses or user identities.

## Areas

| Area | What administrators can do |
| --- | --- |
| Overview | Users, active subscriptions, revenue, refunds, estimated AI spend against the optional cap, OpenAI errors (all from the database) |
| Users | Search, filter, page; suspend/reactivate (suspension also ends sessions); revoke sessions; change role; view a usage summary |
| Subscriptions | Grant a plan for N days (1–366) to give a user AI access without payment, change end date, cancel; edit allowances. Refund-revoked subscriptions are locked |
| Plans | Edit price, duration, allowances and daily limits for **future** purchases. Each change creates an immutable plan version; existing purchases keep the version they bought |
| AI | Manage the OpenAI API keys (`sk-…`, add after a test with OpenAI, order, enable/disable, test, reactivate a rejected key, end a key's rest, remove); add and edit OpenAI models (exact model ID, token limits, prices, fallback); assign a model to Fast/Balanced/Advanced and enable modes; set the optional monthly spending cap, voice limits and reservation hold. See [AI](ai.md) for how key rotation works |
| Usage and costs | Tokens by day, by model and feature, estimated cost, per-user totals |
| Payments | Orders, payment status, invoices (PDF), full and partial refunds through the gateway, webhook delivery status |
| Provider health | OpenAI calls, errors and latency over 24 hours, how many keys are working, and a sanitized call log showing which key served each call |
| Audit log | Who changed what, when, before and after values, and the reason |

## Validation

Configuration changes are validated on the server before they're saved. Examples: plan price
1–100,000,000 paise and duration 1–366 days; daily limits no larger than period limits; model IDs
in a safe character set, with a provider that can't change after creation; a model used by an
enabled mode can't be disabled; enabling a mode needs enabled models; Fast can't be disabled while
another mode is enabled; reservation hold must exceed the AI request timeout; refunds need a reason
and can't exceed what remains refundable. Errors come back per field in the standard error format.

Costs are **estimates** from the prices administrators configure. They aren't provider invoices.

## Audit log

Every change made through the admin API writes an `AuditLogEntry`: administrator (and their email
at the time), action, target, field changes as `{field: [before, after]}`, reason and request ID.
Entries can't be edited or deleted through the application. Sensitive actions (suspension, role
change, cancellation, refunds, plan changes) require a reason in the UI; refunds require one in
the API as well.

Changes made with management commands are **not** recorded in this audit log, except
`promote_admin`. Use the admin panel for routine changes.

## Session revocation

Suspending a user or revoking their sessions blacklists their refresh tokens and records
`sessions_revoked_at`. Access tokens issued at or before that second are rejected on the next
request, so access ends immediately rather than when the 15-minute access token expires. Password
change and reset have the same effect through SimpleJWT's password-hash token claim.

## Refunds

Refunds go through the gateway. The payment row is locked from the amount check until the refund
is recorded, so two simultaneous requests (a double click, two administrators) can't refund the
same payment twice. A full refund revokes the subscription the payment bought; a partial refund
keeps it. See [payments](payments.md).

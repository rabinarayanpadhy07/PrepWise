# Usage limits and enforcement

Enforced on the server in `apps.usage.services`. The browser can't bypass any of these limits.

## Limits

| Limit | Where it's configured | Applies to |
| --- | --- | --- |
| Input tokens per request | AI model `max_input_tokens` | Every request, on each candidate model |
| Output tokens per request | AI model `max_output_tokens` (feature limits are clamped to it) | Every request |
| Total tokens per period | Plan `ai_token_quota` | Every subscriber |
| Tokens per day | Plan `daily_ai_token_limit` (empty = none) | Every subscriber |
| Interviews per period / per day | Plan `interview_quota` / `daily_interview_limit` | Every subscriber |
| Voice seconds per period / per day | Plan `voice_seconds_quota` / `daily_voice_seconds_limit` | Every subscriber |
| Voice seconds per answer | AI platform settings `voice_max_answer_seconds` | Everyone |
| Monthly spending cap (optional) | AI platform settings `monthly_spend_cap` (empty = no cap) | All AI combined |

A subscription copies its allowances from the plan version it was bought under, so changing a
plan only affects future purchases. Days and months follow `DJANGO_TIME_ZONE`. Subscription status
and expiry are checked on every AI request. AI needs an active subscription.

## Reservations and reconciliation

Before an AI call, the worst case (estimated input plus the clamped output limit) and
its estimated cost are **reserved** in one transaction. The transaction holds a row lock on the
user's current subscription and on the platform settings row, which carries the spending cap.
Reserved amounts count toward every limit, so concurrent requests can't overspend.

After the call, the provider-reported usage is **committed** (one `UsageEvent` per reservation, so
a retry can't record it twice) and the rest of the hold is released. A failed call **releases** the
whole reservation. A reservation whose request never finished stops counting after
`reservation_ttl_seconds`. `python manage.py expire_usage_reservations` tidies them up and is safe
to run on a schedule. Interviews reserve one session before generating questions and commit it once
the session exists.

Repeated requests are protected by client idempotency keys (`idempotency_key` on interview start
and analysis creation). A retried request returns the first result instead of calling the provider
again. A request with a key that is still running gets `409 request_in_progress`.

## Errors

Resume analysis, interview questions, answer feedback and resume tailoring don't return these
errors: when AI can't be used (no plan, a limit reached, AI unavailable) they return basic,
rule-based results instead and nothing is charged (see [AI → Basic mode](ai.md#basic-mode)). The
errors below reach the user only from AI-only requests, such as importing an uploaded resume into
the resume maker.

Limit errors use the standard envelope plus a `limit` object:

```json
{"error": {"code": "daily_token_limit_reached", "message": "...",
           "limit": {"kind": "ai_tokens", "scope": "daily", "limit": 50000,
                     "used": 49800, "requested": 9000, "resets_at": "2026-10-28T00:00:00+05:30"},
           "request_id": "..."}}
```

| Code | Status |
| --- | --- |
| `subscription_required`, `subscription_expired` | 403 |
| `token_allowance_exhausted`, `daily_token_limit_reached` | 403 |
| `interview_allowance_exhausted`, `daily_interview_limit_reached` | 403 |
| `voice_allowance_exhausted`, `daily_voice_limit_reached` | 403 |
| `ai_input_too_large` | 400 |
| `platform_budget_exhausted`, `ai_not_configured` | 503 |
| `ai_rate_limited` (every OpenAI key is resting) | 429 |
| `ai_mode_unavailable` | 400 |

## The spending cap is a safeguard, not a guarantee

The cap is optional. It is
enforced against **estimated** costs: OpenAI-reported token counts multiplied by the prices
administrators entered. It is not the provider's invoice. Providers report and bill usage
with delays, may price some tokens differently (for example caching or reasoning tokens), and may
change prices. The cap therefore limits spending as closely as the configured data allows, but it
**can't guarantee** the final amount the provider bills. Set it with headroom, keep model prices up
to date, and use each provider's own budget alerts and hard limits as well.

Recorded token counts are what each provider reported. Costs are always labelled as estimates.

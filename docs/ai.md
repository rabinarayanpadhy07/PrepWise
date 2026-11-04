# AI service layer (OpenAI) and basic mode

All AI calls go through `apps.ai_services.service.run_json_task()`. Feature code (resume
analysis, interview questions, answer evaluation, resume tailoring) never sees an API key, and no
key is ever sent to the browser.

PrepWise's AI runs on **OpenAI only**, and only for users with an **active plan**. A plan is
bought with Razorpay or granted by an administrator (admin panel → Users → *Grant subscription*).
Users never add API keys of their own.

Every feature except *Import from upload* in the resume maker also works **without AI**, using
PrepWise's built-in rules (see [Basic mode](#basic-mode)). Users without a plan always get basic
results; subscribers get them whenever the AI request fails for any reason.

## Provider

| Provider | Adapter | SDK | Structured output |
| --- | --- | --- | --- |
| OpenAI | `OpenAIProvider` | `openai` (Responses API, `store=False`) | `text.format` strict `json_schema` |

The adapter implements `generate_json()`, `verify_key()` (lists models) and `check_model()`
(retrieves the model). It uses the official SDK with an explicit timeout
(`AI_REQUEST_TIMEOUT_SECONDS`) and **one** SDK-level retry for transient failures. Errors are mapped
to service errors:

| OpenAI response | Service error | What happens |
| --- | --- | --- |
| 429 (rate limit, or `insufficient_quota`: no credit left) | `ai_rate_limited` | The key rests and the next key is tried |
| 401 | `ai_key_rejected` | The key is set aside and the next key is tried |
| 404 | `ai_model_unavailable` | Try the mode's fallback model, if allowed |
| 400, 403, 409, 422 | `ai_request_rejected` | Treated as `ai_provider_unavailable` |
| 5xx, network errors | `ai_provider_unavailable` | Try the fallback model, if allowed |

Responses that stop at the output limit (`max_output_tokens`), are refused, or hit the content
filter are errors. Error messages never include OpenAI's responses, prompts, user content or keys.
The adapter is tested against the SDK with a mocked HTTP transport
(`apps/ai_services/tests/test_providers.py`).

## Administrator configuration (admin panel → AI, `/admin/ai`)

1. **OpenAI API keys.** Add one or more keys from
   [platform.openai.com/api-keys](https://platform.openai.com/api-keys). Keys start with `sk-`
   (`sk-proj-…`, `sk-svcacct-…`); anything else is refused before OpenAI is called. Each key is
   tested with OpenAI before it's saved, stored encrypted, and never shown again (only its last four
   characters). Usage is billed to the key's OpenAI account. Keys are used in the order shown;
   reorder, disable, test, reactivate or remove them at any time.
2. **Models.** Add each OpenAI model with its exact `model_id` (for example `gpt-5-mini`),
   per-request `max_input_tokens` / `max_output_tokens`, and prices per million tokens (prices only
   feed cost estimates and the optional spending cap). Optionally set a **fallback** model that
   costs no more on both prices. The model must support structured outputs (`json_schema`).
3. **AI modes.** Fast (the default), Balanced and Advanced are seeded **disabled**. For each mode,
   choose its model, tick *Use the model's fallback when it's busy* if allowed, then enable it.
4. **Limits.** An optional monthly spending cap (empty means no cap), the per-answer voice cap and
   the reservation TTL.

Until at least one key works and one mode is enabled, every user gets basic results.

## Key rotation

For each request, `_call_with_keys()` walks the enabled, non-rejected keys in priority order:

- A key that gets **429** from OpenAI (rate limit, or no credit left) *rests* for that model:
  1 minute, doubling on each further limit hit (2, 4, 8 … minutes) up to 1 hour. A successful call
  clears the rest. Rests are per model, because OpenAI's rate limits are per model.
- A key OpenAI **rejects** (or that can't be decrypted) is marked `rejected` and skipped until an
  administrator tests it successfully or clicks *Reactivate*.
- If every key is resting for the model, the request raises `ai_rate_limited`; the mode's fallback
  model (if allowed) is tried next with the same keys.
- If no key works at all, the request raises `ai_not_configured`.

The admin panel shows each key's status (Ready, Resting until …, Rejected, Disabled), request
count, last use and last problem. Every provider call is logged in `ProviderCallLog` with the key
that served it (outcome codes and timings only).

## Request flow

1. **Access.** The user must have an active subscription (`subscription_required` /
   `subscription_expired` otherwise).
2. **Mode and model.** The request's `ai_mode`, then the user's saved preference, then Fast. The
   mode must be enabled and have an enabled model, and at least one key must be usable.
3. **Candidates.** The model, then (only if the mode allows it) its fallbacks, each costing no more.
4. **Per candidate:** check the per-request input limit, reserve the worst case (estimated input
   plus the clamped output limit) against the plan's allowances and the optional spending cap,
   call OpenAI (rotating keys as above), then commit the reported tokens and estimated cost, or
   release the reservation if the call failed.
5. **Validation.** The JSON response is validated by a serializer. Truncated, refused or invalid
   output is never repaired or filled in.
6. **Basic fallback.** If the caller passed a `basic` function, any `AIServiceError` or
   `UsageError` from steps 1–5 returns the basic result instead (logged as a warning with the
   error code). Without `basic`, the error is raised to the user.

Token usage is recorded per call in `usage.UsageEvent` (`source` is always `platform`, tied to the
subscription). `estimated_cost` comes from the administrator-entered prices.

## Basic mode

Basic results come from deterministic, offline rules. They record **no usage**, are saved with
`ai_source = "basic"` (empty `ai_provider`, `ai_mode` and `ai_model`), and are labelled
**"Basic (not AI-generated)"** in the app and in PDF reports. Their output has the same shape as the
AI's and passes the same serializers.

| Feature | Basic engine | What it does |
| --- | --- | --- |
| Interview questions | `apps/interviews/basic.py` `basic_questions()` | Opening question, then a mix from a role-specific bank (engineering, data, product, design, marketing, sales, finance, HR/people, operations, support, general), skill questions from the job description and resume, and behavioural questions |
| Answer feedback | `apps/interviews/basic.py` `basic_evaluation()` | Scores the same five rubric competencies from simple checks: length, relevance to the question, STAR structure, numbers, reasoning words, skills named, filler words |
| Resume analysis | `apps/resumes/basic.py` `basic_analysis()` | Keyword matching between resume and job description: matched skills with the resume line as evidence, missing skills with importance from the job's wording (required / nice to have), recommendations, and likely questions |
| Resume tailoring | `apps/resumes/basic.py` `basic_tailoring()` | Only reorders skill and tech lists so the job's skills come first; never adds or rewords anything |
| Import from upload | — | AI only; without AI the user sees why |

The skills vocabulary, keyword matching and role detection live in
`apps/ai_services/basic_text.py`.

Interviews: subscribers' AI interviews are held against the plan's interview allowance; a session
that falls back to basic questions gives the interview back. Users without a plan can start any
number of basic interviews, by text or voice. Voice time counts against a subscriber's plan while
some is left; after that (and without a plan) voice answers are still accepted, since transcription
runs in the browser.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/ai/access/` | Plan status, AI modes, usage and voice allowance |
| GET, PATCH | `/api/v1/ai/preference/` | The user's default AI mode |
| GET, POST | `/api/v1/admin/ai/keys/` | List keys (masked) / add a key (tested first) |
| PATCH, DELETE | `/api/v1/admin/ai/keys/<id>/` | Label, order, enable/disable, `reactivate`, `clear_cooldowns` / remove |
| POST | `/api/v1/admin/ai/keys/<id>/test/` | Test a saved key with OpenAI |

Every key change is recorded in the audit log with the key's last four characters only.

The tailoring endpoint (`POST /api/v1/built-resumes/<id>/tailor/`) returns `source`: `platform`
(AI), `basic`, or empty when there was nothing to edit.

## Secrets and encryption

| Variable | Purpose |
| --- | --- |
| `AI_KEY_ENCRYPTION_KEYS` | `<id>:<base64 32-byte key>` entries, comma-separated; the first encrypts. Required in production. |
| `AI_REQUEST_TIMEOUT_SECONDS` | OpenAI request timeout (keep proxy and Gunicorn timeouts above it) |

Keys are encrypted with AES-256-GCM (`apps/ai_services/crypto.py`). Each value is
`v1.<key id>.<nonce>.<ciphertext+tag>` with a random 96-bit nonce. The encryption keys live only in
the environment, never in PostgreSQL. Generate an entry with
`python manage.py generate_encryption_key --id 2026b`.

**Rotating the encryption key:**

1. Put the new entry **first** and keep the old one after it:
   `AI_KEY_ENCRYPTION_KEYS=2026b:<new>,2026a:<old>`. Deploy.
2. Run `python manage.py rotate_encryption_keys --dry-run`, then `python manage.py rotate_encryption_keys`.
3. When it reports nothing left to re-encrypt, remove the old entry and deploy again.

Losing every encryption key makes the saved OpenAI keys unreadable; they are then marked rejected
and must be added again in the admin panel.

## Switching from Gemini (migration `ai_services` 0007)

Migration `0007_openai_platform_keys` removes the Gemini model rows and every saved key (all of
them were Gemini keys), and switches off any AI mode that used a Gemini model. Provider call logs
and usage history are kept. After migrating, add an OpenAI key and model in the admin panel and
enable a mode; until then everyone gets basic results.

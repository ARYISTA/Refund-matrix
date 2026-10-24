# API spec

Source of truth for shapes: `backend/app/contracts/api.py`. Phase 8 implements these routes;
FastAPI's generated OpenAPI must match this document.

## Conventions

- Auth is a JWT in an httpOnly cookie. User id and role come from the token, never from a body or query.
- Request bodies reject unknown fields (422). A client cannot send an `amount`, `customer_id` or `role`.
- Error bodies are `{ "code": "snake_case", "message": "A plain human sentence." }`.
- Customers who ask for a case that is not theirs get **404**, not 403, so ids cannot be probed.
- All timestamps are timezone-aware ISO 8601. All money is integer paise, currency `INR`.

## Endpoints

| Method | Path | Role | Request | Response |
|---|---|---|---|---|
| POST | `/auth/login` | public | `LoginRequest` | `UserOut`, sets cookie |
| POST | `/auth/logout` | any | none | 204, clears cookie |
| GET | `/auth/me` | any | none | `UserOut` |
| POST | `/chat` | customer | `ChatRequest` | SSE stream of `SSEEvent` |
| GET | `/cases/{id}` | customer (own) or reviewer | none | `CaseOut` (customer) or `ReviewerCaseOut` (reviewer) |
| GET | `/reviews` | reviewer | none | `list[ReviewQueueItem]` |
| POST | `/reviews/{id}/decision` | reviewer | `ReviewDecisionRequest` | `ReviewDecisionResponse` |

`/auth/*` is not in the original endpoint list, but a JWT cookie needs a way to be issued. It is the
minimum needed and adds nothing else.

## POST /chat (SSE)

Body: `{ "message": "...", "case_id": null | "<uuid>" }`. Message length is 1 to 2000 characters.
`case_id: null` starts a new case; otherwise the message continues that case (must be the caller's own).

Stream events (`event: <type>`, `data: <json>`):

| type | payload | when |
|---|---|---|
| `case` | `case_id` | first event of every turn |
| `node` | `node`, `status` | each node started, completed or failed |
| `token` | `text` | streamed text of the reply |
| `policy` | `result: PolicyResult` | after policy_check completes |
| `interrupt` | `case_id`, `reason: needs_approval` | graph paused at human_review |
| `done` | `case_id`, `status` | last event of a normal turn |
| `error` | `message` | unrecoverable stream error (plain sentence) |

Order for a turn: `case`, then any mix of `node` / `token` / `policy`, then optionally `interrupt`, then `done`.
After `interrupt` the turn ends with `done` and status `pending_approval`; the graph stays paused until a review decision.

## GET /cases/{id}

Used on page refresh to restore the case panel from `thread_id`.

- Customer: `CaseOut`. Contains messages, timeline, policy result, amount, masked payout and `awaiting_approval`. It has **no** trace id, no audit details and no reviewer identity.
- Reviewer: `ReviewerCaseOut` adds `customer_id`, `audit_events`, `trace_id`, `trace_url`, `handoff_reason` and `review`.

## GET /reviews

Reviewer queue. Lists cases with status `pending_approval` (needs a decision) and `handed_off`
(LLM or provider failure; a human must follow up). Handed-off cases are visible but cannot be decided here.

## POST /reviews/{id}/decision

Body: `{ "decision": "approve" | "deny", "note": null | "..." }`.

| Situation | Result |
|---|---|
| Case is `pending_approval`, first decision | 200, `idempotent_replay: false`; graph resumes |
| Same decision repeated (double-click, retry) | 200, `idempotent_replay: true`; nothing happens, no second payout |
| Opposite decision after one was recorded | 409 `decision_conflict` |
| Case not `pending_approval` (paid, rejected, answered, handed off) | 409 `case_not_pending` |
| Caller is not a reviewer | 403 |
| Unknown case id | 404 |

Idempotency must hold under concurrent requests (row lock or compare-and-set on status), not just sequential ones.

## Limits and security (Phase 8)

- Rate limit per user and per IP (`RATE_LIMIT_PER_MINUTE`), 429 with `Retry-After`.
- Message cap 2000 characters, note cap 500.
- CORS allowlist from `CORS_ALLOWED_ORIGINS`; credentials allowed only for those origins.
- PII is masked before any external LLM call and before anything goes to tracing. No secrets or PII in logs.

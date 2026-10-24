# Refund Matrix Stage 1 Audit

Date: 2026-10-23

## Stage 1 execution results

| Check | Result | Evidence |
| --- | --- | --- |
| `docker compose up -d db` | **Passed** | `refund-matrix-db` is healthy on `127.0.0.1:5432`. |
| `python -m alembic upgrade head` | **Passed** | Migrations applied, including the `vector` extension. |
| `python -m app.db.seed --reset` | **Passed** | Seed completed with demo users and data. |
| Backend startup and health | **Passed** | Uvicorn started and `/health` returned `{"status":"ok"}`. |
| `python -m pytest -q` | **Passed** | `336 passed, 2 skipped in 48.73s` against fresh Postgres. |
| `npm exec tsc -- --noEmit` | **Passed** | Exit code 0. |
| `npm run lint` | **Blocked by project** | `frontend/package.json` has no `lint` script; this remains a tooling gap. |
| `npm run build` | **Passed** | Vite production build completed successfully. |

No secrets or values from `.env` are included in this audit. The backend configuration
fallback and Postgres model/seed drift found during validation were corrected and verified.

## Prioritized issues and planned fixes

### P0 — Required to make the requested flow possible

1. **The frontend is a single mixed customer/reviewer screen, not two guarded portals.**
   - Location: [frontend/src/App.tsx](../frontend/src/App.tsx):5-76.
   - Root cause: local `view` state switches between customer chat, reviewer queue, and
     detail; there is no router, `/login` entry route, role-based redirect, or server-backed
     refresh restoration.
   - Planned fix: add route guards and `/login`, `/app`, and `/company` portal routes;
     restore `/auth/me` before rendering; redirect each role to its own home; remove
     reviewer controls from customer UI.

2. **The API has no company overview, all-case search/filter, case detail, or settings
   endpoints.**
   - Location: [backend/app/api.py](../backend/app/api.py):101-273.
   - Root cause: only the legacy review queue, customer-safe case endpoint, and decision
     endpoint exist.
   - Planned fix: add manager-only overview, cases, manager case detail, review queue,
     and threshold settings endpoints with explicit response contracts and authorization.

3. **There is no database-backed, versioned policy threshold.**
   - Locations: [backend/app/policy/engine.py](../backend/app/policy/engine.py):166-197,
     [backend/app/db/models.py](../backend/app/db/models.py), and migrations `0001`-`0005`.
   - Root cause: the engine reads the static `policy.yaml` value and stores only the YAML
     policy version; there is no `policy_settings` table, threshold audit event, or
     per-case threshold snapshot.
   - Planned fix: add versioned settings and audit tables/migration, load the latest
     threshold inside the workflow, snapshot it on each case, validate manager updates,
     and regenerate the current-limits knowledge chunk.

4. **Manager naming and authorization are still based on `reviewer`.**
   - Locations: [backend/app/contracts/enums.py](../backend/app/contracts/enums.py):94-97,
     [backend/migrations/versions/0005_phase8_users.py](../backend/migrations/versions/0005_phase8_users.py):34,
     [frontend/src/api.ts](../frontend/src/api.ts):20-24.
   - Root cause: the persisted role, API contracts, and UI types all expose `reviewer`.
   - Planned fix: preserve the stored value if migration risk is high, but expose it as
     `manager` in API/UI contracts and labels; otherwise perform a safe role migration.

5. **Review decisions are not transactionally idempotent.**
   - Location: [backend/app/api.py](../backend/app/api.py):239-273.
   - Root cause: the case is changed to `paid` before graph resume, and concurrent
     requests can both pass the pending check; the response always reports
     `idempotent_replay=False`. Payout uniqueness helps only after the race reaches the
     gateway.
   - Planned fix: lock the pending case in a transaction, persist the decision/result,
     resume only once, and return the existing result for repeat requests. Keep the
     payout gateway's case/idempotency uniqueness as a second safety boundary.

### P1 — Correctness and reliability defects

6. **The graph's interrupt/resume path needs an integration check against the installed
   LangGraph/PostgresSaver APIs.**
   - Locations: [backend/app/graph/workflow.py](../backend/app/graph/workflow.py):259-276,
     369-375, and [backend/app/api.py](../backend/app/api.py):168-217.
   - Root cause: the implementation recreates a checkpointer context for every request and
     resumes with `Command`, but Stage 1 has not been able to exercise `setup()`, checkpoint
     persistence, or `thread_id` behavior against Postgres.
   - Planned fix: use a startup/lifetime-managed checkpointer where supported, verify
     `setup()` and async/sync compatibility with the installed versions, and add mocked
     graph tests for auto-approve, pause/resume approve, deny, and reject.

7. **The workflow does not explicitly persist enough case data for the requested company
   detail view.**
   - Locations: [backend/app/db/models.py](../backend/app/db/models.py):20-70 and
     [backend/app/contracts/api.py](../backend/app/contracts/api.py):128-174.
   - Root cause: durable business-case storage contains identity/status/trace only; policy,
     decision, threshold version, review result, and payout details are primarily in the
     checkpoint state. That makes company search/counts and historical snapshots fragile.
   - Planned fix: add the minimal case snapshot columns/relations required for indexed
     company queries and preserve the exact policy/threshold version used.

8. **The API CORS policy omits `PUT` and the SSE response is missing production buffering
   safeguards.**
   - Location: [backend/app/api.py](../backend/app/api.py):61-69 and 217.
   - Root cause: allowed methods are only `GET, POST`; the future settings update needs
     `PUT`. The stream has no `Cache-Control: no-cache`, `X-Accel-Buffering: no`, or
     explicit keepalive strategy.
   - Planned fix: add only the required methods/headers and SSE-safe response headers,
     then verify streaming through Vite proxy.

9. **The seed data does not meet the new demo invariant for every customer.**
   - Location: [backend/app/db/seed.py](../backend/app/db/seed.py):1-27 and seed order
     definitions later in the file.
   - Root cause: the existing scenarios are useful but Rahul/Priya do not each have the
     requested guaranteed low-value and high-value pair suitable for every new flow.
   - Planned fix: retain existing scenarios and add/adjust deterministic orders so each
     demo customer owns at least one low- and one high-value order; keep Argon2 hashes and
     idempotent seed behavior.

10. **LLM provider and test configuration currently fail the existing suite.**
    - Locations: [backend/app/config.py](../backend/app/config.py):31-67,
      [backend/app/llm/retry.py](../backend/app/llm/retry.py):64-102, and
      [backend/app/llm/tools.py](../backend/app/llm/tools.py) (tool input validation).
    - Root cause: test-created `Settings` requires `jwt_secret` even for non-auth tests;
      the observed suite also fails timeout/cancellation behavior, order-id validation,
      and tracing defaults. Groq model availability and structured JSON behavior have not
      been verified against the live API because the required environment/database startup
      gate is unavailable.
    - Planned fix: correct the smallest test/config contract mismatch without weakening
      production secret requirements, fix cancellation/timeout and strict tool validation,
      then run the focused tests and the full suite. Verify configured Groq models through
      the provider/API without printing credentials.

11. **The frontend API contracts and status model are incomplete for the requested UX.**
    - Locations: [frontend/src/types.ts](../frontend/src/types.ts):1-61 and
      [frontend/src/api.ts](../frontend/src/api.ts):1-53.
    - Root cause: types expose policy/audit details to the generic case model, use
      `reviewer`, lack overview/settings/case search calls, and do not model logout or
      manager-safe detail separately.
    - Planned fix: split customer and manager DTOs, add typed portal endpoints,
      customer-friendly statuses/replies, logout, and refresh-safe state handling.

### P2 — Tooling and maintainability

12. **Frontend linting is not configured.**
    - Location: [frontend/package.json](../frontend/package.json):6-14.
    - Root cause: there is no `lint` script or lint dependency, so the requested check
      cannot run.
    - Planned fix: add the repository-standard lightweight ESLint configuration and script
      only if it is compatible with the existing Vite/TypeScript setup, then run it.

13. **Documentation is behind the requested product shape.**
    - Location: [docs/dev-setup.md](dev-setup.md), especially the API and frontend sections.
    - Root cause: docs describe a reviewer queue and one mixed frontend rather than
      manager/customer portals, threshold management, and the required test matrix.
    - Planned fix: update README and setup documentation after implementation and verified
      test commands, as required by Stage 5.

## Stage 1 gate

The runtime and backend Stage 1 gates now pass: Postgres is healthy, migrations and seed
complete, the API health check succeeds, Ruff passes, and the full backend suite passes.
Frontend typecheck and build also pass. The only remaining Stage 1 tooling gap is the absent
frontend `lint` script; it is recorded as P2 and should be addressed without delaying the
portal work.

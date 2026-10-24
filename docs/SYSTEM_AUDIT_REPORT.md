# Refund Matrix: Comprehensive System Audit & Health Report

**Date:** 2026-10-24  
**Audit Type:** Full-Stack Functional & Architectural Audit (Frontend + Backend)  
**Execution Environment:** Windows 11, Node.js v24.22.1, Python 3.12.15 (.venv with `uv`), PostgreSQL 16 + pgvector (Docker on port 5433), Groq LLM API.

---

## 1. Executive Summary

A comprehensive, end-to-end audit was conducted across both the `backend` (FastAPI + LangGraph + SQLAlchemy + Alembic + Groq) and `frontend` (React 19 + TypeScript + Vite) repositories. Every core business flow was executed against the live system, including:
1. **Customer Authentication & Authorization**
2. **Customer Chat & LLM Intake / Policy Evaluation**
3. **Deterministic Automated Approvals & Mock Payout Execution**
4. **Velocity Escalation & Human Review Interruption via LangGraph**
5. **Manager Portal: Review Queue, Approval Decisions, and Case Resumption**
6. **Dynamic Refund Threshold Configuration & Ingestion**

### Core Finding
The core deterministic policy engine and LangGraph interrupt/resume workflow are **architecturally sound and functioning accurately**. However, significant **disconnections** exist between layers:
- The **RAG vector search** subsystem (`app/rag`) is built and tested, but **never invoked by the API/agent runtime**.
- The frontend **ignores streaming tokens** in real-time, relying only on post-stream polling.
- The manager cases view **lacks search and filtering UI controls** despite backend support.
- Customers have **no visibility into their order history** before initiating a chat.

---

## 2. Live Flow Execution Results

| Flow | Initiator | Order & Scenario | Expected Behavior | Actual Observed Result | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Flow 1: Auto-Approval** | Rahul Mehta (`CUS-1002`) | `ORD-1001` (₹2,499.00, Damaged, Captured) | All 7 policy rules pass, amount below threshold, 0 recent refunds. Auto-approves and issues payout. | Policy: `AUTO_APPROVE`. Payout generated (`pout_...`, UTR generated). Status changed to `paid` in ~1.4s. | **PASSED** |
| **Flow 2: Security Isolation** | Asha Verma (`CUS-1001`) | `ORD-1001` (Belongs to Rahul Mehta) | Rule `order_exists_and_owned` fails. Rejects refund immediately. | Policy: `REJECT`. Status: `rejected`. Case closed without payout or escalation. | **PASSED** |
| **Flow 3: Velocity Escalation** | Asha Verma (`CUS-1001`) | `ORD-3001` (₹1,198.00, Defective, 3 prior 30d refunds) | Exceeds 30-day velocity limit (3 refunds). Policy escalates. Workflow pauses. | Rule `refund_velocity` failed. LangGraph called `interrupt()`. Case marked `pending_approval`. | **PASSED** |
| **Flow 4: Manager Decision** | Manager (`reviewer@example.com`) | Case `edaec1d1...` (Asha's escalated case) | Visible in `/reviews` queue. Manager clicks Approve. Graph resumes. Payout created. | Appeared in queue. `POST /reviews/:id/decision` returned 200 OK. Graph resumed, payout created, status `paid`. | **PASSED** |
| **Flow 5: Dynamic Threshold** | Manager (`reviewer@example.com`) | Update threshold to ₹6,000 (600,000 paise) | Threshold saved in DB `policy_settings`, audited, and limits chunk regenerated. | Version incremented (v1 -> v2), audited in `policy_setting_audits`, `upsert_current_limits` executed. | **PASSED** |

---

## 3. Backend Audit: Functioning vs Disconnected vs Missing

### 3.1 What is Fully Functioning
1. **Deterministic Policy Engine (`app/policy/engine.py`)**:
   - Zero LLM dependency in financial decision-making. Evaluates 7 hard rules in strict precedence: `REJECT > ESCALATE > AUTO_APPROVE`.
   - Never exposes internal fraud flags or PII in customer reason text.
2. **LangGraph State Graph & Durability (`app/graph/workflow.py`)**:
   - State checkpointing into Postgres (`checkpoints` and `checkpoint_blobs`) via `AsyncPostgresSaver`.
   - Native pause on human review via `interrupt()` and resume via `Command(resume=decision)`.
3. **Database Models & Migrations (`migrations/versions`)**:
   - Clean Alembic migrations (0001 through 0006) supporting pgvector, case tracking, audit events, and versioned policy settings.
4. **Mock Payout Simulation (`app/payout.py`)**:
   - Strict idempotency key per case (`refund:{case_id}`).
   - Deterministic SHA-256 payout ID and 12-digit UTR generation.
   - Masks customer bank account numbers (`XXXXXXXX1234`).
5. **Auth & Role-Based Access Control (`app/security.py`)**:
   - HttpOnly, SameSite Lax JWT cookies with argon2 password hashing.
   - Enforces `require_role(UserRole.CUSTOMER)` and `require_role(UserRole.REVIEWER)`.

---

### 3.2 What is Disconnected / Partially Implemented
1. **CRITICAL: RAG Vector Search is Completely Disconnected from Runtime**:
   - **File:** [backend/app/llm/tools.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/llm/tools.py#L110-L122) & [backend/app/api.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/api.py#L309-L313)
   - **Issue:** `hybrid_search()` exists in `app/rag/retrieve.py` and documents are ingested in pgvector, but **no `search_policy` tool is registered** in `_TOOL_FACTORIES`. Furthermore, `api.py` instantiates `WorkflowDependencies` without `tool_context=...`.
   - **Impact:** When a customer asks non-refund policy questions ("What is the return window?", "Can I return opened cosmetics?"), the resolution agent cannot query the RAG database and must answer blind.
2. **Resolution Agent Cannot Look Up Orders in API**:
   - **File:** [backend/app/api.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/api.py#L309) & [backend/app/graph/workflow.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/graph/workflow.py#L326-L340)
   - **Issue:** `deps.tool_context` is `None` in the API handler. In `workflow.py`, `resolution()` checks `if deps.tool_context is None:` and falls back to plain `generate_text()`.
   - **Impact:** The `lookup_order_status` tool is never handed to the resolution agent during live API chat.
3. **Trace URL Hardcoded to `None`**:
   - **File:** [backend/app/api.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/api.py#L406)
   - **Issue:** `ReviewerCaseOut.trace_url` is hardcoded to `None`, even when Langfuse tracing is active. Reviewers cannot jump to trace timelines.
4. **CORS Headers Missing `PUT` Method in Preflight**:
   - **File:** [backend/app/api.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/api.py#L82)
   - **Issue:** `allow_methods=["GET", "POST"]` omits `"PUT"`. While Vite proxy or same-origin requests pass, direct cross-origin requests to `PUT /company/settings` fail CORS preflight.
5. **Per-Request Selector Event Loop Spawning**:
   - **File:** [backend/app/api.py](file:///c:/Users/aryab/Projects/refund-matrix-project/backend/app/api.py#L306-L347)
   - **Issue:** To circumvent Windows `ProactorEventLoop` limitations with psycopg async connections, `api.py` creates a new `SelectorEventLoop` runner and recreates connection pools for every single chat and decision request.
   - **Impact:** High latency and connection churn under load.

---

### 3.3 What is Missing in Backend
1. **Customer Order Listing Endpoint (`GET /customer/orders`)**:
   - Customers have no endpoint to fetch their own orders, items, or status.
2. **Customer Historical Cases Endpoint (`GET /customer/cases`)**:
   - Customers cannot view past refund requests; the API only provides `GET /cases/{case_id}` by direct UUID lookup.
3. **Distributed Rate Limiting**:
   - The current `RateLimiter` uses an in-memory dictionary. A multi-worker deployment (e.g. gunicorn/uvicorn workers) does not share rate limits.

---

## 4. Frontend Audit: Functioning vs Disconnected vs Missing

### 4.1 What is Fully Functioning
1. **Role-Based Portals**:
   - Automatic redirect based on `/auth/me`: Customers to `/app`, Managers to `/company`. Unauthenticated to `/login`.
2. **Authentication & Sign Out**:
   - Seamless session validation with credential persistence.
3. **Manager Dashboard (`ManagerPortal`)**:
   - Live overview metrics: Pending Approval, Auto-processed, Total Refunded.
   - Review queue cards with decision modals (Approve / Deny).
   - Refund threshold input that calls `PUT /company/settings`.
4. **Theme Toggling**:
   - Light and dark theme toggling with `localStorage` persistence.

---

### 4.2 What is Disconnected / Defective in Frontend
1. **Chat SSE Streaming Token Event is Ignored**:
   - **File:** [frontend/src/App.tsx](file:///c:/Users/aryab/Projects/refund-matrix-project/frontend/src/App.tsx#L100-L105)
   - **Issue:** The backend streams `event: token` containing model replies in real time. But `App.tsx` only handles `type === "case"` and `type === "done"`.
   - **Impact:** Users see skeleton loaders throughout the generation instead of live token-by-token streaming, making the UI feel slow even when backend response has started.
2. **Manager Case List Search & Status Filter Controls are Missing**:
   - **File:** [frontend/src/App.tsx](file:///c:/Users/aryab/Projects/refund-matrix-project/frontend/src/App.tsx#L221) & [frontend/src/api.ts](file:///c:/Users/aryab/Projects/refund-matrix-project/frontend/src/api.ts#L24-L30)
   - **Issue:** `getCompanyCases(search, status)` is fully implemented in `api.ts`, but `ManagerPortal` in `App.tsx` renders `<h2>All cases</h2>{cases.map(...)}` without any search input field or status dropdown filter.
3. **Single Case Storage in `localStorage`**:
   - **File:** [frontend/src/App.tsx](file:///c:/Users/aryab/Projects/refund-matrix-project/frontend/src/App.tsx#L20)
   - **Issue:** `localStorage.getItem("thread_id")` stores only a single string. If a customer starts a new refund, previous chat threads become inaccessible in the UI.
4. **Manager Detail Navigation Bug**:
   - **File:** [frontend/src/App.tsx](file:///c:/Users/aryab/Projects/refund-matrix-project/frontend/src/App.tsx#L225)
   - **Issue:** The `Back` button in `Detail` executes `history.back()`. If the user accessed the detail view directly or refreshed, `history.back()` can exit the app entirely rather than restoring the Manager view state.
5. **Customer Timeline Component is Never Rendered**:
   - **File:** [frontend/src/App.tsx](file:///c:/Users/aryab/Projects/refund-matrix-project/frontend/src/App.tsx#L178-L180)
   - **Issue:** `CasePanel` checks `"timeline" in caseData`. Because `CustomerCaseOut` intentionally hides internal execution nodes, the Customer panel only displays a single text string (`reply`), leaving the timeline completely blank for customers.

---

### 4.3 What is Missing in Frontend
1. **Component Modularization**:
   - `App.tsx` contains 228 lines containing 10+ components (`Login`, `CustomerChat`, `CasePanel`, `Timeline`, `PolicyCard`, `ApprovalPanel`, `Queue`, `ManagerPortal`, `Detail`, `StatusBadge`). It needs decomposition into `components/` and `pages/`.
2. **Order Picker Widget**:
   - Customers should have a visual list of their eligible orders to click "Refund this item" instead of having to manually type `ORD-XXXX`.
3. **Pagination**:
   - Both Review Queue and All Cases list will overflow without pagination or infinite scroll.

---

## 5. Security & Safety Compliance

| Security Invariant | Status | Mechanism |
| :--- | :--- | :--- |
| **No LLM Money Movement** | **Compliant** | LLM outputs are strictly interpreted into JSON DTOs. Payout execution is exclusively performed by `payout()` node via deterministic Python code. |
| **Customer Cross-Account Isolation** | **Compliant** | All queries enforce `customer_id == user.customer_id`. Tool contexts inject trusted customer ID from JWT, never from user prompt. |
| **Prompt Injection Defense** | **Compliant** | All customer text wrapped in `<<<UNTRUSTED_CUSTOMER_TEXT>>>` delimiters with system boundary instructions. |
| **PII & Bank Detail Protection** | **Compliant** | Bank accounts masked in DB (`account_masked = "XXXX...1234"`). Audit events and tracing strip prompt contents and credentials. |
| **Payout Idempotency** | **Compliant** | Payout gateway uses `refund:{case_id}` as unique idempotency key with database unique constraint. |

---

## 6. Recommended Improvement Roadmap

### Phase 1: High Priority (Disconnections & Fixes)
1. **Wire RAG to Resolution Agent**:
   - Add `search_policy` tool to `backend/app/llm/tools.py`.
   - Pass `ToolContext` in `backend/app/api.py` when building `WorkflowDependencies`.
2. **Hook Up Frontend Token Streaming**:
   - Update `App.tsx` `streamChat` handler to append streamed tokens to the active assistant message in real time.
3. **Add Manager Case Search & Filter UI**:
   - Add search box and status dropdown in `ManagerPortal` connected to `getCompanyCases(search, status)`.
4. **Fix CORS Configuration**:
   - Add `"PUT"` and `"DELETE"` to `allow_methods` in `backend/app/api.py`.

### Phase 2: Medium Priority (UX Enhancements)
1. **Customer Order Selector**:
   - Add `GET /customer/orders` endpoint in backend.
   - Render order cards in Customer Portal with "Request Refund" quick-action.
2. **Modularize Frontend Architecture**:
   - Split `App.tsx` into `features/auth/`, `features/customer/`, `features/manager/`, and `components/ui/`.
3. **Direct Route Handling**:
   - Replace manual `pushState` with React Router or lightweight hash router for robust deep-linking.

### Phase 3: Production Readiness
1. **Checkpointer Connection Lifecycle**:
   - Initialize `AsyncPostgresSaver` once in FastAPI lifespan context rather than per-request instantiation.
2. **Configurable Database Settings**:
   - Expose `return_window_days` and `max_refunds_per_customer_30d` in `policy_settings` table so managers can update them without deploying code.

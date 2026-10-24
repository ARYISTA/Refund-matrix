# Refund Matrix — Agent Developer Guide (`CLAUDE.md`)

This guide provides immediate, complete context for any AI agent working on the **Refund Matrix** codebase. Read this first before analyzing or executing changes.

---

## 1. System Overview & Core Philosophy

Refund Matrix is a multi-agent, deterministic payment refund application featuring two distinct guarded portals:
- **Customer Portal (`/app`)**: Natural language chat interface for refund questions, intake, and customer-safe status tracking.
- **Manager Portal (`/company`)**: Review queue for escalated cases, company-wide metrics, policy threshold management, and manual approve/deny decisions.

### Golden Rule: Separation of LLM vs Money
- **LLMs never decide refunds or touch money.** LLMs are strictly used to interpret messy, untrusted customer text into validated Pydantic structures (`triage`, `refund_intake`, `resolution`, `responder`).
- **The Policy Engine is 100% deterministic Python code** (`app/policy/engine.py`). It enforces hard rules in strict precedence: `REJECT > ESCALATE > AUTO_APPROVE`.
- **Payouts are executed by code only** (`app/payout.py`) after automated policy approval or verified manager authorization.

---

## 2. Directory Layout & Key Files

```
refund-matrix-project/
├── .env                       # Root environment file (never commit secrets)
├── docker-compose.yml         # Postgres 16 + pgvector container
├── start-backend.ps1          # Helper script to launch DB + backend API
├── start-frontend.ps1         # Helper script to launch Vite dev server
├── docs/                      # Architecture docs, specifications, and audit reports
│   ├── SYSTEM_AUDIT_REPORT.md # Comprehensive system audit report (2026-10-24)
│   ├── AUDIT.md               # Prioritized audit from Stage 1
│   ├── api-spec.md            # HTTP endpoint specifications
│   └── state-model.md         # LangGraph state & node transitions
├── backend/                   # FastAPI + LangGraph backend (Python >= 3.12)
│   ├── pyproject.toml         # Dependencies (uv / setuptools)
│   ├── alembic.ini            # Database migration configuration
│   ├── app/
│   │   ├── api.py             # FastAPI endpoints, auth cookies, SSE streaming
│   │   ├── config.py          # Pydantic Settings reading root .env
│   │   ├── security.py        # Argon2 hashing, JWT cookies, role checks
│   │   ├── payout.py          # Deterministic mock payout gateway
│   │   ├── tracing.py         # PII-safe Langfuse adapter
│   │   ├── contracts/         # Pydantic models (api, state, decisions, enums, facts)
│   │   ├── db/                # SQLAlchemy models, sessions, queries, seed fixtures
│   │   ├── graph/             # LangGraph workflow, nodes, checkpointers, interrupts
│   │   ├── llm/               # Groq provider, tools, retry policies, untrusted wrapping
│   │   ├── policy/            # Deterministic policy engine and policy.yaml
│   │   └── rag/               # Vector embeddings, hybrid retrieval, document ingestion
│   ├── migrations/            # Alembic database migrations (0001-0006)
│   └── tests/                 # Pytest test suite (312+ tests)
└── frontend/                  # React 19 + TypeScript + Vite frontend
    ├── package.json           # Dependencies and scripts
    ├── vite.config.ts         # Configured with host: "0.0.0.0", port 5173
    └── src/
        ├── App.tsx            # Main application shell and UI components
        ├── api.ts             # Typed fetch client and SSE streaming handler
        ├── types.ts           # Shared TypeScript interfaces matching backend DTOs
        └── styles.css         # Clean, theme-aware stylesheet
```

---

## 3. Technology Stack & Local Ports

| Component | Technology | Default Port | Notes |
| :--- | :--- | :--- | :--- |
| **PostgreSQL + pgvector** | Docker image `pgvector/pgvector:pg16` | **`5433`** | Bound to `127.0.0.1:5433` (avoids port 5432 conflict on host) |
| **Backend API** | FastAPI + Uvicorn + Python 3.12 | **`8000`** | Base URL: `http://localhost:8000` |
| **Frontend UI** | React 19 + TypeScript + Vite | **`5173`** | Base URL: `http://localhost:5173` |
| **LLM Provider** | Groq API | External | Active model: `qwen/qwen3.8-27b` |

---

## 4. Setup & Running Instructions (Windows PowerShell)

### Prerequisites
- Docker Desktop (must be running)
- `uv` installed (`C:\Users\aryab\.local\bin\uv.exe`)
- Node.js >= 20

### 1. Database
```powershell
docker compose up -d db
```

### 2. Backend Virtualenv & Dependencies
```powershell
cd backend
# Create virtual environment with Python 3.12
& "C:\Users\aryab\.local\bin\uv.exe" venv --python 3.12
# Install packages
& "C:\Users\aryab\.local\bin\uv.exe" pip install -e ".[dev]"
# Apply database migrations
.\.venv\Scripts\alembic.exe upgrade head
# Seed demo fixtures
.\.venv\Scripts\python.exe -m app.db.seed --reset --company-fixture
```

### 3. Frontend Dependencies
```powershell
cd frontend
npm install
```

### 4. Running the Servers
- **Backend**:
  ```powershell
  cd backend
  .\.venv\Scripts\uvicorn.exe app.api:app --reload --port 8000
  ```
- **Frontend**:
  ```powershell
  cd frontend
  npm run dev
  ```
- Or use the root scripts: `.\start-backend.ps1` and `.\start-frontend.ps1`.

---

## 5. Demo Accounts & Credentials

Demo password for all accounts: **`demo-password`**

| Role | Email | Scenario / Purpose |
| :--- | :--- | :--- |
| **Manager** | `reviewer@example.com` | Manager workspace, review queue, company overview, policy settings |
| **Customer 1** | `asha.verma@example.com` | Owns `ORD-3001` - `ORD-3005`. Has 3 recent refunds (triggers velocity escalation) |
| **Customer 2** | `rahul.mehta@example.com` | Owns `ORD-1001` - `ORD-1005`. 0 recent refunds (triggers instant auto-approval) |
| **Customer 3** | `priya.nair@example.com` | Owns `ORD-2001` - `ORD-2005`. Has 2 recent refunds |

---

## 6. Architecture & State Flow

### LangGraph Lifecycle (`backend/app/graph/workflow.py`)
```
                     [START]
                        │
                        ▼
                    [triage] (LLM: classifies intent)
                   /        \
(refund_request)  /          \ (general_question / other)
                 ▼            ▼
         [refund_intake]  [resolution] (Agent answers question)
                 │            │
                 ▼            ▼
           [load_facts]   [responder]
                 │            │
                 ▼            ▼
          [policy_check]    [END]
          /     │      \
(REJECT) /      │       \ (AUTO_APPROVE)
        /   (ESCALATE)   \
       ▼        │         ▼
  [responder]   ▼      [payout] (Processes refund)
       │  [human_review]  │
       ▼        │         ▼
     [END]      ▼      [responder]
             [INTERRUPT]  │
                │         ▼
     (Manager Resumes)  [END]
```

### Key Workflow Details:
- **`triage`**: Uses Groq structured output (`TriageOutput`) to detect `refund_request`, `status_inquiry`, `cancellation`, or `other`.
- **`refund_intake`**: Extracts `order_id`, `items`, and `reason_category`.
- **`load_facts`**: Queries database for verified order facts, payments, items, and 30-day refund counts for that customer.
- **`policy_check`**: Checks policy rules against facts and current dynamic threshold.
  - If `AUTO_APPROVE`: routes to `payout` -> `responder`.
  - If `REJECT`: routes directly to `responder`.
  - If `ESCALATE`: pauses via `interrupt()` in `human_review`.
- **`human_review`**: When resumed with `ReviewDecision(action="approve")`, routes to `payout`; if `"deny"`, routes to `responder`.
- **`payout`**: Invokes `MockPayoutGateway`, generating an idempotency key `refund:{case_id}` and simulated UTR.

---

## 7. Known Architectural Nuances & Gotchas

1. **Groq Model Names**:
   - Older models like `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` were decommissioned or restricted on Groq.
   - **Always use `qwen/qwen3.8-27b`** for both `GROQ_MODEL_TRIAGE` and `GROQ_MODEL_AGENT` in `.env`.
2. **Windows Event Loop (`SelectorEventLoop`)**:
   - `psycopg` async pool on Windows requires `SelectorEventLoop`. In `app/api.py`, graph operations are executed via `asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(...)` inside `asyncio.to_thread`.
3. **Database Port Conflict**:
   - Use `127.0.0.1:5433` for `refund-matrix-db` to prevent conflicts with any pre-existing Postgres instances on 5432.
4. **Vite Host Binding**:
   - `frontend/vite.config.ts` must maintain `server: { host: "0.0.0.0", port: 5173 }` so both `localhost` and `127.0.0.1` resolve smoothly.
5. **Security Isolation**:
   - Never pass customer ID from customer input or LLM output. The authenticated JWT session is the only source of truth.

---

## 8. Validation Commands

Run these to verify integrity before and after changes:

```powershell
# Backend validation
cd backend
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest -q

# Frontend validation
cd frontend
npm run typecheck
npm run build
```

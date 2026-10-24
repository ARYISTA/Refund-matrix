---
name: refund-matrix
description: Comprehensive knowledge base, architecture reference, and operations playbook for the Refund Matrix payment refund workflow project.
---

# Refund Matrix Skill & Operational Cheatsheet

Use this skill whenever working with the **Refund Matrix** multi-agent refund system.

## Quick Reference

| Component | Port | Technology | Entry Point |
| :--- | :--- | :--- | :--- |
| **Backend API** | 8000 | FastAPI, LangGraph, Python 3.12 | `backend/app/api.py` |
| **Frontend UI** | 5173 | React 19, TypeScript, Vite | `frontend/src/App.tsx` |
| **Database** | 5433 | PostgreSQL 16 + pgvector | `refund-matrix-db` (Docker) |
| **LLM Provider** | Groq | `qwen/qwen3.8-27b` | `backend/app/llm/groq_provider.py` |

---

## Service Management Commands

```powershell
# Start Database Container
docker compose up -d db

# Run Backend API Server
cd backend
.\.venv\Scripts\uvicorn.exe app.api:app --reload --port 8000

# Run Frontend Dev Server
cd frontend
npm run dev

# Apply Alembic Migrations
cd backend
.\.venv\Scripts\alembic.exe upgrade head

# Re-seed Demo Fixtures
cd backend
.\.venv\Scripts\python.exe -m app.db.seed --reset --company-fixture
```

---

## Credentials (Password: `demo-password`)

- **Manager**: `reviewer@example.com`
- **Customer (Instant Auto-Approve demo)**: `rahul.mehta@example.com` (Order: `ORD-1001`)
- **Customer (Velocity Escalation demo)**: `asha.verma@example.com` (Order: `ORD-3001`)
- **Customer (Pre-escalated demo)**: `priya.nair@example.com`

---

## Critical System Invariants

1. **Deterministic Financial Boundary**:
   - The LLM interprets customer text into structured data (`order_id`, `reason_category`, `items`).
   - The LLM **never** decides approvals, denials, or payouts.
   - The Policy Engine (`app/policy/engine.py`) is 100% pure Python code.
   - Policy Precedence: `REJECT > ESCALATE > AUTO_APPROVE`.

2. **Customer Security Isolation**:
   - Customer IDs are always read from the JWT session (`current_user`), never from customer input.
   - Cross-account access is strictly blocked at the SQL query level (`order_exists_and_owned`).

3. **LangGraph State & Human Review**:
   - Durable execution is persisted in Postgres via `AsyncPostgresSaver`.
   - When policy evaluates to `ESCALATE`, LangGraph invokes `interrupt()`.
   - The manager resumes paused workflows via `Command(resume=decision)`.

4. **Groq Model Requirement**:
   - Do NOT use `llama-3.1-8b-instant` (decommissioned on Groq).
   - Use `qwen/qwen3.8-27b` for both `GROQ_MODEL_TRIAGE` and `GROQ_MODEL_AGENT`.

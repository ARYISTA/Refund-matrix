# Refund Matrix

Refund Matrix is a two-portal refund workflow:

- Customer portal at `/app` for refund questions and customer-safe statuses.
- Manager portal at `/company` for review, policy settings, cases, audit details, and payouts.

Refund decisions are made by the deterministic policy engine. LLMs help interpret
untrusted customer text but never decide money or payout eligibility.

## Local setup

Requirements: Docker Desktop, Python 3.12 or newer, and Node.js 20 or newer.

1. Copy `.env.example` to `.env` and set the database password, JWT secret, and
   Groq settings. Do not commit `.env`.
2. Start Postgres and pgvector:

   ```powershell
   docker compose up -d db
   ```

3. Install the backend and apply migrations:

   ```powershell
   cd backend
   python -m pip install -e ".[dev]"
   alembic upgrade head
   ```

4. Load the supplied demo company fixture:

   ```powershell
   python -m app.db.seed --reset --company-fixture
   ```

   This creates three customers, one manager, 15 orders, payments, and refund
   scenarios. The demo password for every account is `demo-password`.

5. Start the API in one terminal:

   ```powershell
   uvicorn app.api:app --reload --port 8000
   ```

6. Start the frontend in a second terminal:

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

Open <http://localhost:5173>. Unauthenticated users start at `/login`.

## Demo accounts

| Role | Email |
| --- | --- |
| Customer | `asha.verma@example.com` |
| Customer | `rahul.mehta@example.com` |
| Customer | `priya.nair@example.com` |
| Manager | `reviewer@example.com` |

All use `demo-password`.

The fixture intentionally includes a customer with recent refund velocity, so a
request can remain under review even when its amount is below the threshold.
Amounts below the current threshold are automatically paid only when every hard
policy rule passes and no review escalation applies.

## Validation

Backend checks:

```powershell
cd backend
python -m ruff check .
python -m pytest -q
```

Database-backed tests additionally require `TEST_DATABASE_URL` and use a
throwaway database. Live Groq tests require `GROQ_API_KEY` and configured model
names; ordinary tests do not call Groq.

Frontend checks:

```powershell
cd frontend
npm run typecheck
npm run lint
npm run build
```

For setup details, RAG ingestion, tracing, and environment variables, see
[docs/dev-setup.md](docs/dev-setup.md). The prioritized code audit is in
[docs/AUDIT.md](docs/AUDIT.md).

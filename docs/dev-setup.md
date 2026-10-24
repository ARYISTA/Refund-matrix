# Dev setup (Phases 1 through 9)

Needs Docker, Python 3.12+.

```bash
cp .env.example .env                  # then set POSTGRES_PASSWORD and DATABASE_URL to match
docker compose up -d db               # Postgres 16 + pgvector on 127.0.0.1:5432

cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head                  # create tables, enable pgvector
python -m app.db.seed                 # demo data (add --reset to reload with fresh dates)
# Supplied 15-order company scenarios:
python -m app.db.seed --reset --company-fixture
```

Phase 4 adds the LangGraph workflow and durable checkpoints. Use the same Postgres database
for application data and checkpoints. The checkpoint tables are created by the application
with `make_postgres_checkpointer(DATABASE_URL)` when the graph worker starts. Keep that
checkpointer context open for the lifetime of the compiled graph:

```python
from app.graph import build_graph, make_postgres_checkpointer

with make_postgres_checkpointer(settings.database_url.get_secret_value()) as checkpointer:
    graph = build_graph(dependencies, checkpointer=checkpointer)
```

Every invocation must include `configurable.thread_id`; resuming an approval uses
`Command(resume=ReviewDecision(...).model_dump(mode="json"))`. The graph writes PII-free
node transitions to `audit_events`, while LangGraph checkpoints retain the full state.

Run checks:

```bash
cd backend
pytest -q                             # policy engine, contracts, config, logging
TEST_DATABASE_URL=postgresql+psycopg://refund:<password>@localhost:5432/postgres pytest -q   # adds DB tests
ruff check . && mypy app
```

`TEST_DATABASE_URL` points at any database on the server; each test run creates and drops its own
throwaway database, so it never touches your dev data. Without it the DB tests are skipped.

Notes:
- `.env` is git-ignored. Never commit it.
- `DATABASE_URL` must use the `postgresql+psycopg://` driver (psycopg 3).
- Seed dates are relative to when you seed. If demo orders age out of the 30-day window, run `python -m app.db.seed --reset`.
- `--company-fixture` loads the supplied `ORD-1001` through `ORD-3005` scenarios while
  preserving the same demo login accounts and password hashes. It also stores expected
  delivery dates for in-transit and overdue undelivered-order checks.

## RAG (Phase 5)

The `0003` migration creates `knowledge_documents` and `knowledge_chunks`, including a
pgvector HNSW index. Ingestion supports `policy.yaml` and Markdown, text, and PDF files in
`docs/knowledge`. The ingestion path is idempotent by source path and content hash:

```python
from pathlib import Path
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import make_engine
from app.rag import HashEmbeddingProvider, ingest_knowledge

settings = get_settings()
with Session(make_engine()) as session:
    ingest_knowledge(
        session,
        policy_path=Path("app/policy/policy.yaml"),
        documents_path=Path("../docs/knowledge"),
        embedder=HashEmbeddingProvider(settings.embedding_dim),
    )
```

`hybrid_search` combines pgvector cosine similarity with PostgreSQL full-text ranking and
returns an empty list below `RAG_MIN_SIMILARITY`. The hash embedder is deterministic for
tests and local smoke runs; a production embedding provider can implement the same
`EmbeddingProvider` interface.

## Tracing (Phase 6)

Tracing is disabled by default. To enable Langfuse, set `TRACING_ENABLED=true`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, and optionally
`TRACING_SAMPLE_RATE`. The tracing adapter exports only bounded operational metadata,
model role, token counts, latency, and error class. Prompts, customer messages, model
output, tool arguments, credentials, and payment details are never sent to Langfuse.

## Payout simulation (Phase 7)

The default `PAYOUT_GATEWAY=mock` gateway persists processed payouts in PostgreSQL.
The idempotency key is derived from the case id, so retrying or double-clicking approval
returns the original payout result rather than creating a second payout. Only a masked
beneficiary account is stored and returned.

## API and security (Phase 8)

Start the API with:

```bash
cd backend
uvicorn app.api:app --reload --port 8000
```

Authentication uses an HttpOnly `refund_session` cookie containing a short-lived HS256 JWT.
Passwords are hashed with Argon2. Customer identity is read from the token and is never
accepted from request bodies. Reviewer routes require the reviewer role. In production set
`COOKIE_SECURE=true`, use a strong `JWT_SECRET` of at least 32 characters, and set an explicit
`CORS_ALLOWED_ORIGINS` allowlist.

## Frontend (Phase 9)

The frontend is a Vite + React + TypeScript application with customer chat, reviewer
queue, case detail, live SSE updates, policy checks, approval confirmation, and light/dark
themes. It restores the last case from the `thread_id` saved in browser storage.

Start it in a second terminal while the API is running:

```bash
cd frontend
npm install
npm run dev
```

The development server runs at `http://localhost:5173`. Set `VITE_API_BASE_URL` when the
API is hosted elsewhere. Frontend checks and the production bundle can be verified with:

```bash
npm run build
```

The UI uses mocked/no live model calls during its build; opening a real customer chat
requires the API, a logged-in customer session, and the configured provider.

The demo seed creates these local accounts, all using password `demo-password`:

- Customer: `asha.verma@example.com`
- Customer: `rahul.mehta@example.com`
- Customer: `priya.nair@example.com`
- Reviewer: `reviewer@example.com`

## LLM (Phase 3)

Set `GROQ_API_KEY`, `GROQ_MODEL_TRIAGE` and `GROQ_MODEL_AGENT` in `.env` (see `.env.example`). Model names are
config, not code. Nothing else in the app needs them yet; the provider only reads them when it is built.

The unit tests never call Groq. To smoke-test the real API (costs a few tokens):

    GROQ_API_KEY=... GROQ_MODEL_TRIAGE=... GROQ_MODEL_AGENT=... pytest tests/test_llm_groq_live.py

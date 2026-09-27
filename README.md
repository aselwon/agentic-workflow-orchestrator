# OpsAgent

OpsAgent is a small agentic orchestrator for support workflows. It plans a bounded sequence of steps, calls allowlisted tools, persists run memory, pauses for human approval, and records an audit trail. The default `MOCK_LLM=1` planner is deterministic and works without an external model provider.

## Quick start with Docker Compose

Docker with Compose is required. Start the application and run the end-to-end mock demonstration:

```bash
docker compose up --build -d --wait
docker compose exec app python scripts/demo.py --base-url http://localhost:8000
```

Open the UI at <http://localhost:8088>. To use another host port:

```bash
OPSAGENT_PORT=8090 docker compose up --build -d --wait
python scripts/demo.py --base-url http://localhost:8090
```

The demo starts a run for `delayed`, waits for its human approval checkpoint, approves the frozen tool call, and completes the workflow. Inside the application container, the service listens on port `8000`.

## Local development

Python 3.12 and `uv` are required. Start PostgreSQL and install the development dependencies:

```bash
uv sync --extra dev
docker compose up -d db
```

In a separate terminal, start the mock order service:

```bash
uv run uvicorn app.mock_orders:app --port 9001
```

Start the API and HTMX UI in another terminal:

```bash
uv run uvicorn app.main:app --port 8088
```

Run the demo in a third terminal:

```bash
uv run python scripts/demo.py --base-url http://localhost:8088
```

By default, the local API connects to PostgreSQL at `localhost:54329` and the mock order service at `127.0.0.1:9001`. The Compose `mock-orders` service is not published to the host, so run `app.mock_orders` locally or set `MOCK_ORDER_URL` when running the API on the host.

## Workflow and safety

The deterministic plan processes the first matching ticket and uses these tools in order:

1. `search_tickets(query)`
2. `get_order(order_id)` through the mock HTTP service
3. `create_draft_reply(ticket_id, text)`
4. `schedule_followup(ticket_id, when)`

Ticket contents are untrusted input and cannot change the plan. Tool names and arguments are checked against an allowlist and Pydantic schemas; unknown fields are rejected. The application creates a saved reply draft and follow-up record. It does not send messages.

Runs move through `queued`, `running`, `waiting_human`, `succeeded`, or `failed`. The audit log records structured planning events, tool actions and observations, state changes, human decisions, and errors. Draft creation pauses for human approval when confidence is below `CONFIDENCE_THRESHOLD` (default `0.85`). Approval applies to the exact frozen arguments identified by `approval_id`.

The worker locks each run and executes at most one step per transaction. Local tool effects are transactional: if an action fails, its local effects roll back while the attempted action and error remain in the audit trail. Idempotency receipts are stored for each run step; retries with the same fingerprint return the saved result. `RUN_TIMEOUT_SECONDS` includes time waiting for approval, and `MAX_STEPS` bounds execution. Queued, running, and approval-waiting runs are persisted and can resume after an application restart.

`MOCK_LLM=0` fails closed. This MVP does not configure an external model provider. The ticket text is rendered with template autoescaping, and write endpoints enforce a browser-origin boundary. Local Compose binds the database and application ports to localhost and uses demo-only credentials. The application has no authentication; these defaults are for local development only.

Production use would require authentication, database migrations, a durable outbox, downstream idempotency, and integration with real support systems. The MVP does not provide cryptographic protection against audit-log tampering.

## API

- `GET /health` — health check including the database connection.
- `GET /api/tools` — allowlisted tools and their JSON Schemas.
- `POST /api/runs` with `{"query":"delayed"}` — create a run.
- `GET /api/runs` and `GET /api/runs/{run_id}` — list runs or retrieve run memory and audit events.
- `POST /api/runs/{run_id}/decision` with `{"approval_id":"...","decision":"approve"}` or `"reject"` — submit a human decision.
- `GET /` and `GET /runs/{run_id}` — HTMX interface.

## Configuration

These environment variables can also be provided through a local `.env` file:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://opsagent:opsagent_local@localhost:54329/opsagent` | PostgreSQL connection |
| `MOCK_ORDER_URL` | `http://127.0.0.1:9001` | Mock order service URL |
| `MOCK_LLM` | `1` | Enable the deterministic offline planner |
| `CONFIDENCE_THRESHOLD` | `0.85` | Confidence threshold for human approval |
| `MAX_STEPS` | `8` | Maximum steps per run |
| `RUN_TIMEOUT_SECONDS` | `900` | Run deadline, including human approval wait |
| `TOOL_TIMEOUT_SECONDS` | `5` | Tool call timeout |
| `WORKER_ENABLED` | `1` | Enable the background worker |
| `WORKER_INTERVAL_SECONDS` | `0.25` | Worker polling interval |
| `OPSAGENT_PORT` | `8088` | Docker Compose host port for the app |

## Tests and linting

Tests use SQLite by default. For PostgreSQL integration checks, create a dedicated test database so the application worker does not compete with the tests on its live database:

```bash
docker compose exec db createdb -U opsagent opsagent_test
TEST_DATABASE_URL=postgresql+psycopg://opsagent:opsagent_local@localhost:54329/opsagent_test uv run pytest -q
```

Run the standard checks with:

```bash
uv run pytest
uv run ruff check .
```

## Project structure

- `app/main.py` — FastAPI endpoints, HTMX UI, and browser-origin boundary.
- `app/engine.py` — run state machine, worker, human approval, audit, and transactions.
- `app/planner.py` — deterministic four-tool plan.
- `app/tools.py` — allowlisted tools, argument validation, mock HTTP calls, and idempotency receipts.
- `app/models.py` — SQLAlchemy models for runs, events, approvals, drafts, and follow-ups.
- `app/templates/` and `app/static/` — Jinja templates and the HTMX interface, including locally vendored HTMX.
- `tests/` — workflow tests and optional PostgreSQL scenarios.

The initial Docker image build and `uv sync` may need network access to download dependencies.

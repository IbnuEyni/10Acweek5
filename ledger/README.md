# The Ledger — Agentic Event Store

PostgreSQL-backed event store for the loan origination domain, built with `asyncpg` and `pydantic`.

## Requirements

- Python 3.14+
- PostgreSQL 14+ (running locally or via Docker)
- [uv](https://github.com/astral-sh/uv)

## Install

```bash
# Clone and enter the project
cd ledger

# Install all dependencies (including dev) with uv
uv sync --all-groups
```

## Configuration

Copy the example env file and set your database URL:

```bash
cp .env.example .env
```

`.env` format:

```
DATABASE_URL=postgresql://<user>@/<dbname>?host=/var/run/postgresql
```

## Run Migrations

Apply the schema to your target database:

```bash
psql $DATABASE_URL -f src/schema.sql
```

For the test database (created automatically by the test fixtures):

```bash
createdb ledger_test   # only needed once
```

The `conftest.py` fixture drops and recreates all tables before each test run — no manual migration needed for tests.

## Run the Test Suite

```bash
uv run pytest
```

Run a specific test file:

```bash
uv run pytest tests/test_concurrency.py -v
uv run pytest tests/test_aggregates.py -v
uv run pytest tests/test_store_basics.py -v
```

## Project Structure

```
src/
  schema.sql                  — PostgreSQL schema (events, event_streams, projection_checkpoints, outbox)
  event_store.py              — EventStore: append, load_stream, load_all, stream_version,
                                archive_stream, get_stream_metadata
  models/
    events.py                 — Pydantic models: BaseEvent, StoredEvent, StreamMetadata
  aggregates/
    base.py                   — Aggregate base class (load → validate → append pattern)
    loan_application.py       — LoanApplicationAggregate with forward-only state machine
    agent_session.py          — AgentSessionAggregate with Gas Town + confidence floor invariants
  commands/
    handlers.py               — handle_submit_application, handle_credit_analysis_completed
tests/
  test_concurrency.py         — Double-decision concurrency test (version 3 race)
  test_aggregates.py          — Domain invariant tests (state machine, confidence floor, Gas Town)
  test_store_basics.py        — EventStore unit + integration tests
```

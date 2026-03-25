# The Ledger — Agentic Event Store & Enterprise Audit Infrastructure

PostgreSQL-backed event store for the loan origination domain.
Implements a full CQRS/ES stack with MCP server, cryptographic audit chain,
Gas Town agent memory reconstruction, and counterfactual what-if analysis.

## Requirements

- Python 3.14+
- PostgreSQL 14+
- [uv](https://github.com/astral-sh/uv)

## Install

```bash
cd ledger
uv sync --all-groups
```

## Configuration

```bash
cp .env.example .env
# Edit .env and set DATABASE_URL
```

`.env` format:
```
DATABASE_URL=postgresql://<user>@/<dbname>?host=/var/run/postgresql
```

## Database Provisioning

```bash
# Create the databases (only needed once)
createdb ledger
createdb ledger_test

# Apply schema to the production database
psql $DATABASE_URL -f src/schema.sql
```

The test fixtures drop and recreate all tables automatically — no manual migration needed for tests.

## Run the Test Suite

```bash
# All 166 tests
uv run pytest

# Specific phases
uv run pytest tests/test_store_basics.py -v       # Phase 1 — Event Store
uv run pytest tests/test_aggregates.py -v         # Phase 2 — Domain Logic
uv run pytest tests/test_projections.py -v        # Phase 3 — Projections & Daemon
uv run pytest tests/test_upcasting.py -v          # Phase 4 — Upcasting
uv run pytest tests/test_integrity.py -v          # Phase 4 — Cryptographic Integrity
uv run pytest tests/test_gas_town.py -v           # Phase 4 — Gas Town Recovery
uv run pytest tests/test_mcp_lifecycle.py -v      # Phase 5 — MCP Lifecycle
uv run pytest tests/test_whatif.py -v             # Phase 6 — What-If
uv run pytest tests/test_regulatory.py -v         # Phase 6 — Regulatory Package
uv run pytest tests/test_concurrency.py -v        # Concurrency
uv run pytest tests/test_handlers.py -v           # Command Handlers
```

## Run the Video Demo

Runs all 6 demo steps end-to-end against a fresh database:

```bash
uv run python demo.py
```

## Start the MCP Server

```bash
uv run python -m src.mcp.server
```

The server communicates over stdio (MCP protocol). Connect via any MCP-compatible client
(Claude Desktop, Cursor, etc.) by pointing it at this process.

## Project Structure

```
src/
  schema.sql                        — Full PostgreSQL schema: events, event_streams,
                                      projection_checkpoints, outbox, application_summary,
                                      agent_performance_ledger, compliance_audit_events,
                                      compliance_audit_snapshots

  event_store.py                    — EventStore: append (SELECT FOR UPDATE concurrency),
                                      load_stream, load_all (async generator), stream_version,
                                      archive_stream, get_stream_metadata, outbox writes

  models/
    events.py                       — Pydantic models: all event types, StoredEvent,
                                      StreamMetadata, DomainError

  aggregates/
    base.py                         — Aggregate base: load → validate → append pattern
    loan_application.py             — LoanApplicationAggregate: 9-state machine, all 6 rules
    agent_session.py                — AgentSessionAggregate: Gas Town, model version locking
    compliance_record.py            — ComplianceRecordAggregate: mandatory check tracking
    audit_ledger.py                 — AuditLedgerAggregate: append-only cross-stream audit

  commands/
    handlers.py                     — All command handlers: submit_application,
                                      request_credit_analysis, credit_analysis_completed,
                                      fraud_screening_completed, compliance_check,
                                      generate_decision, human_review_completed,
                                      start_agent_session, approve_application,
                                      decline_application

  projections/
    base.py                         — Projection ABC: handle, get_lag, rebuild
    daemon.py                       — ProjectionDaemon: fault-tolerant batch processing,
                                      per-projection checkpoints, poll_until_caught_up,
                                      get_lag, get_all_lags, rebuild_projection
    application_summary.py          — ApplicationSummary: one row per application, SLO <500ms
    agent_performance.py            — AgentPerformanceLedger: metrics per agent+model_version
    compliance_audit.py             — ComplianceAuditView: temporal query via get_compliance_at,
                                      snapshot strategy (every 10 events), rebuild_from_scratch

  upcasting/
    registry.py                     — UpcasterRegistry: decorator registration, chain walking,
                                      immutable RecordedEvent via dataclasses.replace
    upcasters.py                    — CreditAnalysisCompleted v1→v2, DecisionGenerated v1→v2
                                      with documented inference strategies

  integrity/
    audit_chain.py                  — run_integrity_check(): SHA-256 hash chain, tamper detection
                                      verify_full_chain(): full chain replay
    gas_town.py                     — reconstruct_agent_context(): crash recovery, token budget,
                                      NEEDS_RECONCILIATION detection

  mcp/
    server.py                       — MCP server entry point (stdio transport)
    tools.py                        — 9 MCP tools (command side): submit_application,
                                      request_credit_analysis, record_credit_analysis,
                                      record_fraud_screening, record_compliance_check,
                                      generate_decision, record_human_review,
                                      start_agent_session, run_integrity_check
    resources.py                    — 6 MCP resources (query side): applications/{id},
                                      applications/{id}/compliance, applications/{id}/audit-trail,
                                      agents/{id}/performance, agents/{id}/sessions/{session_id},
                                      ledger/health

  whatif/
    projector.py                    — run_what_if(): counterfactual injection, causal dependency
                                      filtering, rolled-back transaction replay

  what_if/
    projector.py                    — Alias for whatif/projector.py (spec compatibility)

  regulatory/
    package.py                      — generate_regulatory_package(): complete examination package
                                      with event stream, projection states, integrity verification,
                                      lifecycle narrative, AI participation record, package hash

tests/
  conftest.py                       — Pool + store fixtures, schema bootstrap
  test_store_basics.py              — EventStore unit + integration (43 tests)
  test_aggregates.py                — All 6 business rules (34 tests)
  test_handlers.py                  — Command handlers end-to-end (22 tests)
  test_concurrency.py               — Double-decision + combined concurrent invariants (3 tests)
  test_projections.py               — Projections, daemon, SLO tests (14 tests)
  test_upcasting.py                 — Upcaster registry + immutability (6 tests)
  test_integrity.py                 — Hash chain + tamper detection (8 tests)
  test_gas_town.py                  — Crash recovery + reconciliation (7 tests)
  test_mcp_lifecycle.py             — Full MCP lifecycle, zero direct Python calls (7 tests)
  test_whatif.py                    — Counterfactual scenarios (6 tests)
  test_regulatory.py                — Regulatory package (11 tests)
```

## MCP Tools Reference

| Tool | Description |
|---|---|
| `start_agent_session` | REQUIRED FIRST: declare agent context (Gas Town pattern) |
| `submit_application` | Create a new loan application stream |
| `request_credit_analysis` | Advance application Submitted → AwaitingAnalysis |
| `record_credit_analysis` | Record completed credit analysis (requires active session) |
| `record_fraud_screening` | Record fraud screening result (requires active session) |
| `record_compliance_check` | Record a compliance rule pass or fail |
| `generate_decision` | Generate final decision recommendation |
| `record_human_review` | Record human reviewer's final decision |
| `run_integrity_check` | Run SHA-256 hash chain integrity check |

## MCP Resources Reference

| Resource URI | Description |
|---|---|
| `ledger://applications/{id}` | Current application state from ApplicationSummary projection |
| `ledger://applications/{id}/compliance` | Compliance record; supports `?as_of=<ISO timestamp>` |
| `ledger://applications/{id}/audit-trail` | Full audit trail; supports `?from=&to=` position range |
| `ledger://agents/{id}/performance` | Aggregated agent metrics from AgentPerformanceLedger |
| `ledger://agents/{id}/sessions/{session_id}` | Full agent session replay (Gas Town memory) |
| `ledger://ledger/health` | Projection lag metrics for all projections |

## Query Examples

### Get current application state
```
ledger://applications/demo-abc123
```

### Time-travel compliance query
```
ledger://applications/demo-abc123/compliance?as_of=2025-01-15T12:00:00Z
```

### Agent session replay (Gas Town recovery)
```
ledger://agents/agent-apex-1/sessions/abc12345
```

### Projection health check
```
ledger://ledger/health
```

## Design

See [DESIGN.md](DESIGN.md) for:
- Aggregate boundary justification with concurrency analysis
- Projection strategy (inline vs async, SLO budgets)
- Concurrency error rate estimates under load
- Upcasting inference decisions with error rate analysis
- EventStoreDB comparison
- What I would do differently (dual-write atomicity gap)

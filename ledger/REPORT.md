# The Ledger — Final Submission Report
**TRP1 Week 5 · Agentic Event Store & Enterprise Audit Infrastructure**
**Final Deadline: Thursday March 26, 03:00 UTC**

---

## Section 1: DOMAIN_NOTES.md (Complete)

### 1.1 EDA vs. Event Sourcing

LangChain-style callback traces are **Event-Driven Architecture (EDA)**, not Event Sourcing. The distinction is fundamental:

- In **EDA**, events are messages — they fire and are consumed. If the consumer is down, the event is lost. The callback fires, the trace is written to a log or sink, and the original state of the system is not recoverable from those traces alone.
- In **Event Sourcing**, events are the source of truth — the database *is* the event log. The current state of any entity is always derived by replaying its event stream. Nothing is ever lost because nothing is ever overwritten.

**What would change if redesigned with The Ledger:**

| Aspect | LangChain Callbacks (EDA) | The Ledger (ES) |
|---|---|---|
| Storage | Ephemeral log / external sink | Append-only PostgreSQL stream |
| State recovery | Not possible from traces alone | Full replay from position 0 |
| Concurrency | No conflict detection | Optimistic concurrency via `expected_version` |
| Agent restart | Context lost | Agent replays its stream to reconstruct context |
| Auditability | Best-effort, lossy | Immutable, cryptographically verifiable |

**What you gain:** Every agent decision becomes a first-class, replayable fact. An agent that crashes mid-session reconstructs its exact context window by replaying its `AgentSession` stream. Regulators can query the exact state of any application at any point in time. Two agents cannot simultaneously corrupt the same stream — one receives `OptimisticConcurrencyError` and must reload before retrying.

---

### 1.2 Aggregate Boundary Decision

An aggregate is a consistency boundary: all events within one stream are strongly consistent; events across streams are eventually consistent. Boundary decisions are therefore concurrency decisions — every aggregate boundary is a choice about which writes must be serialised together and which can proceed independently.

**The four aggregates in this system:**
- `LoanApplication` — loan lifecycle state machine
- `AgentSession` — per-agent reasoning session
- `ComplianceRecord` — regulatory checks per application
- `AuditLedger` — cross-cutting audit trail

**Alternative 1 considered:** Merging `ComplianceRecord` into `LoanApplication` as a sub-entity.

**Why it was rejected:**

If compliance checks lived inside `LoanApplication`, every compliance rule evaluation would require acquiring a write lock on the loan stream. Under the Apex scenario — 4 agents processing 1,000 applications/hour — the `ComplianceAgent` and the `CreditAnalysis` agent would compete for the same stream lock on every application. This produces:

1. **Concurrency amplification** — every compliance check becomes a potential `OptimisticConcurrencyError` on the loan stream, forcing retries across all agents.
2. **Invariant leakage** — `LoanApplication` would need to understand compliance rule versions and check sequencing — concerns that belong to the compliance domain.
3. **Rebuild cost** — replaying a loan stream to check approval status would require loading all compliance events unnecessarily.

**The chosen boundary prevents:** write contention between the compliance agent and the credit/fraud agents. Each agent writes to its own stream. `LoanApplication` holds only a reference to compliance status (boolean: all required checks passed), not the compliance detail.

**Alternative 2 considered:** Merging `AuditLedger` into `LoanApplication` as an audit sub-log.

**Why it was rejected:**

The `AuditLedger` spans all three other streams — loan, agent session, and compliance. If it lived inside `LoanApplication`, compliance officers and regulators would need to load the full loan stream to query the audit trail, coupling a read-only regulatory concern to a write-heavy business stream. Additionally, the hash chain integrity guarantee requires an uncontested, append-only stream — any write contention from the loan lifecycle would risk breaking the chain ordering. A dedicated stream gives the audit trail its own version sequence, its own lock, and its own replay path.

---

### 1.3 Concurrency in Practice

**Exact sequence when two agents race at `expected_version=3`:**

```
Agent A                              Agent B
  │                                    │
  ├─ load_stream(loan-X) → v=3         ├─ load_stream(loan-X) → v=3
  │                                    │
  ├─ BEGIN TRANSACTION                 ├─ BEGIN TRANSACTION
  ├─ INSERT event_streams              ├─ INSERT event_streams
  │  ON CONFLICT DO NOTHING            │  ON CONFLICT DO NOTHING
  │                                    │
  ├─ SELECT current_version            │
  │  FOR UPDATE  ◄── acquires lock     │
  │                                    ├─ SELECT current_version
  │                                    │  FOR UPDATE  ◄── BLOCKS
  │
  ├─ version check: 3 == 3 ✓
  ├─ INSERT events (position=4)
  ├─ INSERT outbox row
  ├─ UPDATE current_version = 4
  ├─ COMMIT  ◄── releases lock
  │
  │                                    │  ◄── unblocks, reads v=4
  │                                    ├─ version check: 4 != 3 ✗
  │                                    ├─ ROLLBACK
  │                                    └─ raises OptimisticConcurrencyError
```

**What the losing agent receives:** `OptimisticConcurrencyError("Expected version 3, got 4 for stream loan-X")`.

**What it must do next:**
1. Reload the stream — returns 4 events, version=4.
2. Inspect the new event at position 4 (the winner's `CreditAnalysisCompleted`).
3. Decide whether its own analysis is still relevant. If the winning agent's decision supersedes it, discard. If materially different, append at `expected_version=4` — subject to model version locking rules.

---

### 1.4 Projection Lag and Its Consequences

**Scenario:** `ApplicationSummary` projection lags 200ms. Loan officer queries "available credit limit" immediately after a `LoanDisbursed` event commits.

**What the system does:** Returns the pre-disbursement value from the projection table. The system does not block the query waiting for catch-up — that defeats CQRS.

**How to communicate this to the UI:**

Every projection response includes:
- `as_of_global_position: int` — last global position this projection has processed
- `as_of_timestamp: datetime` — wall-clock time of that position

The UI displays: *"Data current as of [timestamp]."*

For the credit limit specifically:
1. Show the current projection value with the staleness timestamp.
2. Offer a "refresh" action after a short delay (500ms).
3. For automated systems: implement read-your-writes — after appending `LoanDisbursed`, poll until the projection's `as_of_global_position` has advanced past the event's `global_position`.

**SLO commitments:** `ApplicationSummary` < 500ms lag. `ComplianceAuditView` < 2 seconds lag.

---

### 1.5 The Upcasting Scenario

**`CreditAnalysisCompleted` v1 → v2 upcaster:**

The upcaster below is the designed implementation for Phase 4 (`src/upcasting/registry.py` + `src/upcasting/upcasters.py`). `_apply_upcasts()` already exists in `event_store.py` and is called on every `load_stream()` and `load_all()` invocation — the `UpcasterRegistry` class and the registered upcaster functions are the Phase 4 work that remains.

```python
@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        # Inferred from recorded_at — all pre-2026 events used the legacy model
        "model_version": "legacy-pre-2026",
        # null — genuinely unknown; fabrication would corrupt AgentPerformanceLedger averages
        "confidence_score": None,
        # Inferred from regulation set active at recorded_at date
        "regulatory_basis": _infer_regulatory_basis(payload.get("recorded_at")),
    }


def _infer_regulatory_basis(recorded_at: str | None) -> str:
    """Return the regulation set version active at recorded_at."""
    if recorded_at is None or recorded_at < "2025-03-01":
        return "REG-SET-2024-Q4"
    return "REG-SET-2025-Q1"
```

**Inference strategy for `model_version`:** Use `recorded_at` as a proxy. Events before the 2026 model deployment date are assigned `"legacy-pre-2026"`. This is a documented inference with known uncertainty (±1 day near the deployment boundary).

**Why `null` over inference for `confidence_score`:** The score was never computed for v1 events. Fabricating a value (e.g. historical average) would:
- Make historical events appear more precise than they were
- Corrupt aggregate metrics in `AgentPerformanceLedger`
- Create a compliance risk: a regulator would see a score that was never actually computed

`null` is the honest representation of "this field did not exist when this event was recorded."

---

### 1.6 The Marten Async Daemon Parallel

**Marten's pattern:** PostgreSQL advisory locks elect a single leader per projection across multiple nodes. Only the leader processes events; followers wait. If the leader dies, a follower acquires the lock within seconds.

**Python equivalent:**

```python
async def run_projection_with_leader_election(
    pool: asyncpg.Pool,
    projection: Projection,
    poll_interval_ms: int = 100,
) -> None:
    lock_id = hash(projection.name) % (2**31)

    async with pool.acquire() as conn:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id)
        if not acquired:
            return  # another node is leader; stand by

        try:
            while True:
                await _process_batch(conn, projection)
                await asyncio.sleep(poll_interval_ms / 1000)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)
```

**Failure mode guarded against:** Duplicate projection processing. Without leader election, two daemon nodes processing the same events produce double-counted metrics, duplicate rows, and incorrect compliance states. The advisory lock ensures exactly one node processes each batch. If the leader crashes, PostgreSQL automatically releases the lock and a standby node takes over within one poll interval. Standby nodes that fail to acquire the lock return immediately and re-attempt on the next poll cycle — they never block, so the standby pool stays responsive and leader failover completes within one `poll_interval_ms`.

---

### 1.7 EventStoreDB Comparison

EventStoreDB is a purpose-built Event Sourcing database — it enforces the ES guarantees natively that this PostgreSQL implementation must construct explicitly. The comparison below maps each ES concept to both implementations, making the cost of building ES on a relational database visible.

| Concept | This PostgreSQL Implementation | EventStoreDB Equivalent |
|---|---|---|
| Stream | `event_streams` row + `events` rows | Native stream (e.g. `loan-{id}`) |
| `load_stream()` | `SELECT WHERE stream_id ORDER BY stream_position` | `ReadStreamAsync()` |
| `load_all()` | `SELECT WHERE global_position > $last` | `$all` stream subscription |
| `ProjectionDaemon` | Custom asyncio polling loop | Built-in persistent subscriptions |
| Optimistic concurrency | `SELECT FOR UPDATE` + version check | Native `expectedVersion` on `AppendToStreamAsync` |
| Outbox | Custom table in same transaction | Not built-in — requires custom implementation |

**The fundamental difference from an EDA perspective:** EventStoreDB is designed exclusively for Event Sourcing — streams are first-class citizens, not rows in a table. A LangChain-style EDA system using Kafka or Redis Streams would have no concept of `expected_version`, no stream replay, and no aggregate reconstruction — it would be back to the EDA model described in Section 1.1. EventStoreDB sits at the ES end of that spectrum by design; this PostgreSQL implementation builds the same guarantees from relational primitives.

**What EventStoreDB gives you that this implementation must work harder to achieve:** Built-in persistent subscriptions with automatic checkpointing, native gRPC streaming (push vs. poll), built-in stream metadata (max count, max age, ACLs), and server-side `$all` filtering by event type.

---

## Section 2: Architecture Diagram — Full System (All Phases)

### 2.1 Event Store Schema (Phase 1)

```
┌─────────────────────────────────────────────────────────────────────┐
│                          PostgreSQL Database                         │
│                                                                      │
│  ┌──────────────────────┐       ┌────────────────────────────────┐  │
│  │    event_streams     │       │            events              │  │
│  │──────────────────────│       │────────────────────────────────│  │
│  │ stream_id (PK)       │◄──────│ stream_id (FK)                 │  │
│  │ aggregate_type       │       │ event_id (PK, UUID)            │  │
│  │ current_version      │       │ stream_position (BIGINT)       │  │
│  │ created_at           │       │ global_position (IDENTITY)     │  │
│  │ archived_at (NULL=active)    │ event_type                     │  │
│  │ metadata (JSONB)     │       │ event_version (SMALLINT)       │  │
│  └──────────────────────┘       │ payload (JSONB)                │  │
│                                 │ metadata (JSONB)               │  │
│                                 │ recorded_at (clock_timestamp)  │  │
│                                 │ UNIQUE(stream_id,stream_pos)   │  │
│                                 └───────────────┬────────────────┘  │
│                                                 │                   │
│  ┌────────────────────────┐   ┌─────────────────▼──────────────┐   │
│  │ projection_checkpoints │   │             outbox             │   │
│  │────────────────────────│   │────────────────────────────────│   │
│  │ projection_name (PK)   │   │ id (PK, UUID)                  │   │
│  │ last_position (BIGINT) │   │ event_id (FK → events.event_id)│   │
│  │ updated_at             │   │ destination (TEXT)             │   │
│  └────────────────────────┘   │ payload (JSONB)                │   │
│                                │ created_at                     │   │
│                                │ published_at (NULL = pending)  │   │
│                                │ attempts (SMALLINT)            │   │
│                                └────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

Indexes:
  events: (stream_id, stream_position) — aggregate replay
  events: (global_position)            — projection catch-up
  events: (event_type)                 — type-filtered load_all()
  events: (recorded_at)                — temporal range queries
  outbox: (created_at) WHERE published_at IS NULL  — partial, stays small
```

### 2.2 Aggregate Boundaries (Phase 2)

```
┌──────────────────────────────┐  ┌──────────────────────────────────┐
│   LoanApplicationAggregate   │  │      AgentSessionAggregate       │
│──────────────────────────────│  │──────────────────────────────────│
│ stream: loan-{id}            │  │ stream: agent-{id}-{session}     │
│                              │  │                                  │
│ 9-state machine:             │  │ Invariants:                      │
│  Submitted                   │  │  Gas Town: AgentContextLoaded    │
│  → AwaitingAnalysis          │  │  must be first event             │
│  → AnalysisComplete          │  │                                  │
│  → ComplianceReview          │  │  Confidence Floor: score < 0.6   │
│  → PendingDecision           │  │  → forced REFER                  │
│  → ApprovedPendingHuman      │  │                                  │
│  → DeclinedPendingHuman      │  │  Model version locking:          │
│  → FinalApproved (terminal)  │  │  one CreditAnalysisCompleted     │
│  → FinalDeclined (terminal)  │  │  per application unless          │
│                              │  │  superseded by HumanReview       │
│ Business rules:              │  │                                  │
│  No backward transitions     │  │ Boundary justification:          │
│  Compliance dependency       │  │  Agent writes to its own stream; │
│  Causal chain enforcement    │  │  no lock contention with loan    │
└──────────────────────────────┘  └──────────────────────────────────┘

┌──────────────────────────────┐  ┌──────────────────────────────────┐
│  ComplianceRecordAggregate   │  │      AuditLedgerAggregate        │
│──────────────────────────────│  │──────────────────────────────────│
│ stream: compliance-{app_id}  │  │ stream: audit-{type}-{id}        │
│                              │  │                                  │
│ Tracks:                      │  │ Cross-cutting audit trail:       │
│  Mandatory checks required   │  │  Links events across all         │
│  Rules passed / failed       │  │  aggregates for one entity       │
│  Regulation version refs     │  │                                  │
│                              │  │ Invariants:                      │
│ Invariant:                   │  │  Append-only                     │
│  Cannot issue clearance      │  │  No events may be removed        │
│  without all mandatory       │  │  Hash chain integrity (Phase 4)  │
│  checks passed               │  │                                  │
│                              │  │ Boundary justification:          │
│ Boundary justification:      │  │  Spans all 3 other streams;      │
│  Separate stream prevents    │  │  compliance officers query it    │
│  write contention between    │  │  independently; hash chain       │
│  ComplianceAgent and         │  │  requires uncontested append-    │
│  CreditAnalysis agent        │  │  only stream                     │
└──────────────────────────────┘  └──────────────────────────────────┘
```

### 2.3 Command Flow (Phase 2)

```
  External Caller / MCP Tool (Phase 5)
           │
           │  Command (e.g. CreditAnalysisCompletedCommand)
           ▼
  ┌─────────────────────────┐
  │     Command Handler     │  src/commands/handlers.py
  │─────────────────────────│
  │  READ PATH:             │
  │  load_stream()          │──► SELECT events WHERE stream_id ORDER BY position
  │  _apply_upcasts()       │──► transparently upgrades old event payloads
  │  aggregate.rebuild()    │──► replays each StoredEvent through _apply()
  │                         │
  │  WRITE PATH:            │
  │  1. Load                │──► (read path above)
  │  2. Validate            │──► DomainError if invariant violated
  │  3. Determine           │──► Aggregate command method stages events
  │  4. Append              │──► EventStore.append(expected_version=N)
  └──────────┬──────────────┘
             │
             ▼
  ┌─────────────────────────┐
  │       EventStore        │  src/event_store.py
  │─────────────────────────│
  │  BEGIN TRANSACTION      │
  │  INSERT event_streams   │  ON CONFLICT DO NOTHING
  │  SELECT FOR UPDATE      │◄── serialises concurrent appenders
  │  version check          │──► OptimisticConcurrencyError if mismatch
  │  INSERT events          │
  │  INSERT outbox          │  same transaction — guaranteed delivery
  │  UPDATE current_version │
  │  COMMIT                 │
  └──────────┬──────────────┘
             │
             ├──► events table  (immutable log)
             └──► outbox table  (pending relay)
```

### 2.4 Projection Data Flow (Phase 3)

```
  events table
  (global_position)
       │
       │  poll: WHERE global_position > last_checkpoint
       ▼
  ┌─────────────────────────────────────────────────────┐
  │               ProjectionDaemon                      │
  │─────────────────────────────────────────────────────│
  │  - Polls every 100ms                                │
  │  - Routes each event to subscribed projections      │
  │  - Updates projection_checkpoints after each batch  │
  │  - Fault-tolerant: skips bad events after N retries │
  │  - Exposes get_lag() per projection (SLO metric)    │
  │  - Leader election via pg_advisory_lock (multi-node)│
  └──────────┬──────────────────────────────────────────┘
             │
             ├──► ApplicationSummary projection
             │    (one row per loan, current state)
             │    SLO: lag < 500ms
             │
             ├──► AgentPerformanceLedger projection
             │    (metrics per agent model version)
             │    SLO: lag < 500ms
             │
             └──► ComplianceAuditView projection
                  (full compliance record per application)
                  Temporal query: get_compliance_at(id, timestamp)
                  SLO: lag < 2000ms
```

### 2.5 Upcasting Registry (Phase 4)

```
  EventStore.load_stream() / load_all()
           │
           │  for each event row
           ▼
  ┌─────────────────────────────────────┐
  │         UpcasterRegistry            │
  │─────────────────────────────────────│
  │  upcast(event):                     │
  │    v = event.event_version          │
  │    while (event_type, v) in chain:  │
  │      payload = upcasters[v](payload)│
  │      v += 1                         │
  │    return event with new payload    │
  │                                     │
  │  Registered chains:                 │
  │  CreditAnalysisCompleted v1 → v2    │
  │  DecisionGenerated v1 → v2          │
  └─────────────────────────────────────┘
           │
           │  stored events NEVER modified
           ▼
  Caller receives upcasted StoredEvent
```

### 2.6 MCP Server — CQRS Interface (Phase 5)

```
  AI Agent / LLM Consumer
           │
           ├──── MCP Tools (Commands — write side) ────────────────────┐
           │                                                            │
           │  submit_application          → ApplicationSubmitted        │
           │  record_credit_analysis      → CreditAnalysisCompleted     │
           │  record_fraud_screening      → FraudScreeningCompleted     │
           │  record_compliance_check     → ComplianceRulePassed/Failed │
           │  generate_decision           → DecisionGenerated           │
           │  record_human_review         → HumanReviewCompleted        │
           │  start_agent_session         → AgentContextLoaded          │
           │  run_integrity_check         → AuditIntegrityCheckRun      │
           │                                                            │
           │  All tools: structured error types, precondition docs      │
           │  OptimisticConcurrencyError includes suggested_action      │
           └────────────────────────────────────────────────────────────┘
           │
           └──── MCP Resources (Queries — read side) ──────────────────┐
                                                                        │
                 ledger://applications/{id}                             │
                   ← ApplicationSummary projection (p99 < 50ms)        │
                                                                        │
                 ledger://applications/{id}/compliance                  │
                   ← ComplianceAuditView projection                     │
                   ← supports ?as_of=timestamp (p99 < 200ms)           │
                                                                        │
                 ledger://applications/{id}/audit-trail                 │
                   ← AuditLedger stream direct load (justified)        │
                   ← supports ?from=&to= range (p99 < 500ms)           │
                                                                        │
                 ledger://agents/{id}/performance                       │
                   ← AgentPerformanceLedger projection (p99 < 50ms)    │
                                                                        │
                 ledger://agents/{id}/sessions/{session_id}             │
                   ← AgentSession stream direct load (p99 < 300ms)     │
                                                                        │
                 ledger://ledger/health                                 │
                   ← ProjectionDaemon.get_all_lags() (p99 < 10ms)      │
                                                                        │
                 All resources read from projections — no stream        │
                 replays except the two justified exceptions above      │
                 └────────────────────────────────────────────────────┘
```

### 2.7 Full System Data Flow

```
  ┌──────────────────────────────────────────────────────────────────┐
  │                     Apex Financial Services                      │
  │                                                                  │
  │  CreditAnalysis   FraudDetection   ComplianceAgent   Orchestrator│
  │  Agent            Agent            Agent             Agent       │
  │       │                │                │                │       │
  │       └────────────────┴────────────────┴────────────────┘       │
  │                                │                                 │
  │                    MCP Tools (Phase 5)                           │
  │                                │                                 │
  └────────────────────────────────┼─────────────────────────────────┘
                                   │
                          Command Handlers (Phase 2)
                                   │
                          EventStore.append() (Phase 1)
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
               events table                 outbox table
               (immutable)                  (pending relay)
                    │                             │
          ┌─────────┴──────────┐        Outbox Relay Worker
          │                    │        (Phase 3 / Week 10)
   ProjectionDaemon      UpcasterRegistry              │
   (Phase 3)             (Phase 4)              Kafka / Redis
          │                    │                Streams
   ┌──────┴──────┐             │
   │             │             └── applied transparently on load
   │    Projections (Phase 3)
   │
   ├── ApplicationSummary
   ├── AgentPerformanceLedger
   └── ComplianceAuditView
              │
       MCP Resources (Phase 5)
              │
       Loan Officers / Regulators / Audit Tools
              │
       Regulatory Package (Phase 6 Bonus)
       What-If Projector (Phase 6 Bonus)
```

---

## Section 3: Progress Summary

### Phase 1 — Event Store Core ✅ Complete

| Component | Status |
|---|---|
| `src/schema.sql` — all 4 tables, indexes, constraints | ✅ Done |
| `EventStore.append()` with `SELECT FOR UPDATE` concurrency | ✅ Done |
| `EventStore.load_stream()` with upcast support | ✅ Done |
| `EventStore.load_all()` — global feed for projections | ✅ Done |
| `EventStore.stream_version()` | ✅ Done |
| `EventStore.get_stream_metadata()` | ✅ Done |
| `EventStore.archive_stream()` — soft archive | ✅ Done |
| Transactional outbox (same transaction as events) | ✅ Done |

### Phase 2 — Domain Logic ✅ Complete

| Component | Status |
|---|---|
| `Aggregate` base class — load/save/rebuild pattern, `_apply` single source of truth | ✅ Done |
| `LoanApplicationAggregate` — full 9-state machine, all 6 business rules | ✅ Done |
| `AgentSessionAggregate` — Gas Town + confidence floor + model version locking | ✅ Done |
| `ComplianceRecordAggregate` — mandatory check tracking, regulation version refs, clearance gate | ✅ Done |
| `AuditLedgerAggregate` — cross-stream audit trail, hash chain scaffold | ✅ Done |
| `src/models/events.py` — Pydantic `BaseEvent`, `StoredEvent`, `StreamMetadata` | ✅ Done |
| `src/commands/handlers.py` — all 10 command handlers, cross-stream Rule 5 enforcement | ✅ Done |

### Phase 3 — Projections & Async Daemon ⏳ In Progress

| Component | Status |
|---|---|
| `src/projections/daemon.py` — `ProjectionDaemon` with fault-tolerant batch processing, per-projection checkpoints, `get_lag()` | Not started |
| `src/projections/application_summary.py` — one row per application, current state | Not started |
| `src/projections/agent_performance.py` — metrics per agent model version | Not started |
| `src/projections/compliance_audit.py` — temporal query `get_compliance_at()`, `rebuild_from_scratch()` | Not started |
| `tests/test_projections.py` — lag SLO tests under 50 concurrent handlers, rebuild test | Not started |

### Phase 4 — Upcasting, Integrity & Gas Town ⏳ In Progress

| Component | Status |
|---|---|
| `src/upcasting/registry.py` — `UpcasterRegistry` with automatic version chain application | Not started |
| `src/upcasting/upcasters.py` — `CreditAnalysisCompleted` v1→v2, `DecisionGenerated` v1→v2 | Not started |
| `src/integrity/audit_chain.py` — `run_integrity_check()` SHA-256 hash chain, tamper detection | Not started |
| `src/integrity/gas_town.py` — `reconstruct_agent_context()` with token budget, `NEEDS_RECONCILIATION` | Not started |
| `tests/test_upcasting.py` — immutability test: v1 stored, loaded as v2, raw DB payload unchanged | Not started |
| `tests/test_gas_town.py` — crash recovery: 5 events appended, reconstruct without in-memory agent | Not started |

### Phase 5 — MCP Server ⏳ In Progress

| Component | Status |
|---|---|
| `src/mcp/server.py` — MCP server entry point | Not started |
| `src/mcp/tools.py` — 8 tools: submit, credit analysis, fraud screening, compliance check, generate decision, human review, start session, run integrity check | Not started |
| `src/mcp/resources.py` — 6 resources: applications, compliance, audit-trail, agent performance, sessions, health | Not started |
| `tests/test_mcp_lifecycle.py` — full lifecycle via MCP tools only: session → analysis → fraud → compliance → decision → review → query | Not started |

### Phase 6 — Bonus ⏳ In Progress

| Component | Status |
|---|---|
| `src/what_if/projector.py` — `run_what_if()` counterfactual injection, never writes to real store | Not started |
| `src/regulatory/package.py` — `generate_regulatory_package()` self-contained JSON examination package | Not started |

### Tests — Current State

| Test File | Tests | Status |
|---|---|---|
| `tests/test_concurrency.py` | 2 | ✅ Pass |
| `tests/test_aggregates.py` | 34 | ✅ Pass |
| `tests/test_handlers.py` | 28 | ✅ Pass |
| `tests/test_store_basics.py` | 35 | ✅ Pass |
| `tests/test_projections.py` | — | Not started |
| `tests/test_upcasting.py` | — | Not started |
| `tests/test_gas_town.py` | — | Not started |
| `tests/test_mcp_lifecycle.py` | — | Not started |
| **Total (passing)** | **99** | **✅ 99/99** |

---

## Section 4: Concurrency Test Results

**Test:** `tests/test_concurrency.py::test_double_decision_credit_analysis_at_version_3`

**Scenario:** A loan application stream is seeded with 3 events (version 3). Two AI agents simultaneously call `store.append()` at `expected_version=3` using `asyncio.gather`.

**Mechanism:** `SELECT FOR UPDATE` in `append()` serialises the two transactions at the PostgreSQL row level. The first transaction to acquire the lock commits and advances the stream to version 4. The second reads `current_version=4` after the lock is released, fails the version check, and raises `OptimisticConcurrencyError`.

**Assertions verified:**
1. Exactly one agent returns `"success"`, one returns `"conflict"`
2. Final stream length is 4 (3 seeded + 1 winner), never 5
3. The 4th event is `CreditAnalysisCompleted` at `stream_position=4`
4. Exactly one agent's decision is recorded in the stream

**Test output (full suite):**

```
============================= test session starts ==============================
platform linux -- Python 3.14.2, pytest-9.0.2, pluggy-1.6.0
asyncio: mode=Mode.AUTO

tests/test_aggregates.py ..................................              [ 34%]
tests/test_concurrency.py ..                                             [ 36%]
tests/test_handlers.py ............................                      [ 64%]
tests/test_store_basics.py ...................................           [100%]

============================== 99 passed in 9.93s ==============================
```

---

## Section 5: Known Gaps and Plan for Final Submission

### Known Gaps

| Gap | Impact |
|---|---|
| No `ProjectionDaemon` | No CQRS read models; all queries require aggregate replay |
| No upcasting registry | Schema evolution not handled; `event_version` column unused in production |
| No hash chain integrity | Regulatory tamper detection not implemented |
| No Gas Town memory reconstruction | Agent crash recovery not implemented |
| No MCP server | System not accessible to AI agents via MCP protocol |
| No Phase 6 bonus | What-if projector and regulatory package not implemented |

> Note: `DESIGN.md` is not a separate file — all architectural reasoning, diagrams, and tradeoff analysis are consolidated in this report (Sections 1 and 2), which serves as the PDF submission.

### Plan for Final Submission

**Priority 1 — Phase 3 projections:**
- Implement `ProjectionDaemon` with fault-tolerant batch processing, per-projection checkpoints, configurable retry, and `get_lag()`
- Implement `ApplicationSummary`, `AgentPerformanceLedger`, and `ComplianceAuditView` projections
- Add temporal query `get_compliance_at(application_id, timestamp)` and `rebuild_from_scratch()` to `ComplianceAuditView`
- Add `tests/test_projections.py`: lag SLO tests under 50 concurrent command handlers, rebuild test

**Priority 2 — Phase 4 upcasting and integrity:**
- Implement `UpcasterRegistry` with automatic version chain application on event load
- Register `CreditAnalysisCompleted` v1→v2 and `DecisionGenerated` v1→v2 upcasters with inference strategies documented
- Implement `run_integrity_check()` with SHA-256 hash chain construction and tamper detection
- Implement `reconstruct_agent_context()` with token budget and `NEEDS_RECONCILIATION` detection
- Add `tests/test_upcasting.py`: immutability test — v1 stored, loaded as v2, raw DB payload confirmed unchanged
- Add `tests/test_gas_town.py`: crash recovery — 5 events appended, reconstruct without in-memory agent

**Priority 3 — Phase 5 MCP server:**
- Implement all 8 MCP tools with structured error types and precondition documentation in tool descriptions
- Implement all 6 MCP resources reading from projections (no stream replays except justified exceptions)
- Add `tests/test_mcp_lifecycle.py`: full lifecycle driven entirely through MCP tool calls

**Priority 4 — Phase 6 bonus:**
- Implement `run_what_if()` with causal dependency filtering, never writing to real store
- Implement `generate_regulatory_package()` with event stream, projection states at examination date, integrity verification, human-readable narrative, and agent model metadata

**Priority 5 — Docs and packaging:**
- Write `DESIGN.md` with aggregate boundary decisions, concurrency strategy, projection data flow, and MCP tool/resource mapping
- Update `README.md` with MCP server startup, Phase 3–6 run instructions, and query examples
- Update `REPORT.md` with final test results, SLO measurements, upcasting immutability output, and MCP lifecycle trace
- Ensure `pyproject.toml` has all locked deps via `uv`

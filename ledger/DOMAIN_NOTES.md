# DOMAIN_NOTES.md — The Ledger: Agentic Event Store

## 1. EDA vs. Event Sourcing

**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this EDA or ES? If redesigned using The Ledger, what exactly would change and what would you gain?

**Answer:**

LangChain-style callback traces are **Event-Driven Architecture (EDA)**, not Event Sourcing. The distinction is fundamental:

- In EDA, events are **messages** — they fire and are consumed. If the consumer is down, the event is lost. The callback fires, the trace is written to a log or a sink, and the original state of the system is not recoverable from those traces alone.
- In Event Sourcing, events are the **source of truth** — the database *is* the event log. The current state of any entity is always derived by replaying its event stream. Nothing is ever lost because nothing is ever overwritten.

**What would change if redesigned with The Ledger:**

| Aspect | LangChain Callbacks (EDA) | The Ledger (ES) |
|---|---|---|
| Storage | Ephemeral log / external sink | Append-only PostgreSQL stream |
| State recovery | Not possible from traces alone | Full replay from position 0 |
| Concurrency | No conflict detection | Optimistic concurrency via `expected_version` |
| Agent restart | Context lost | Agent replays its stream to reconstruct context |
| Auditability | Best-effort, lossy | Immutable, cryptographically verifiable |

**What you gain:** Every agent decision becomes a first-class, replayable fact. An agent that crashes mid-session can reconstruct its exact context window by replaying its `AgentSession` stream. Regulators can query the exact state of any application at any point in time. Two agents cannot simultaneously corrupt the same stream — one will receive `OptimisticConcurrencyError` and must reload before retrying.

---

## 2. Aggregate Boundary Decision

**Question:** Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?

**The four aggregates in this system:**
- `LoanApplication` — loan lifecycle state machine
- `AgentSession` — per-agent reasoning session
- `ComplianceRecord` — regulatory checks per application
- `AuditLedger` — cross-cutting audit trail

**Alternative considered:** Merging `ComplianceRecord` into `LoanApplication` as a sub-entity.

**Why it was rejected:**

If compliance checks lived inside `LoanApplication`, every compliance rule evaluation would require acquiring a write lock on the loan stream. Under the Apex scenario — 4 agents processing 1,000 applications/hour — the `ComplianceAgent` and the `CreditAnalysis` agent would be competing for the same stream lock on every application. This produces:

1. **Concurrency amplification** — every compliance check becomes a potential `OptimisticConcurrencyError` on the loan stream, forcing retries across all agents.
2. **Invariant leakage** — the `LoanApplication` aggregate would need to understand compliance rule versions, regulation sets, and check sequencing — concerns that belong to the compliance domain, not the loan lifecycle domain.
3. **Rebuild cost** — replaying a loan stream to check its current status would require loading all compliance events, even when the query is only about the loan's approval state.

**The chosen boundary prevents:** write contention between the compliance agent and the credit/fraud agents. Each agent writes to its own stream. The `LoanApplication` aggregate holds only a reference to the compliance status (a boolean: all required checks passed), not the compliance detail. The detail lives in `ComplianceRecord`.

---

## 3. Concurrency in Practice

**Question:** Two AI agents simultaneously call `append_events` with `expected_version=3`. Trace the exact sequence of operations. What does the losing agent receive, and what must it do next?

**Exact sequence:**

```
Agent A                              Agent B
  │                                    │
  ├─ load_stream(loan-X)               ├─ load_stream(loan-X)
  │  ← [e1, e2, e3], version=3         │  ← [e1, e2, e3], version=3
  │                                    │
  ├─ BEGIN TRANSACTION                 ├─ BEGIN TRANSACTION
  │                                    │
  ├─ INSERT event_streams              ├─ INSERT event_streams
  │  ON CONFLICT DO NOTHING            │  ON CONFLICT DO NOTHING
  │                                    │
  ├─ SELECT current_version            │
  │  FROM event_streams                │
  │  WHERE stream_id = loan-X          │
  │  FOR UPDATE  ◄── acquires lock     │
  │                                    ├─ SELECT current_version
  │                                    │  FROM event_streams
  │                                    │  WHERE stream_id = loan-X
  │                                    │  FOR UPDATE  ◄── BLOCKS (lock held by A)
  │
  ├─ version check: 3 == 3 ✓
  ├─ INSERT events (stream_position=4)
  ├─ INSERT outbox row
  ├─ UPDATE current_version = 4
  ├─ COMMIT  ◄── releases lock
  │
  │                                    │  ◄── unblocks, reads current_version=4
  │                                    │
  │                                    ├─ version check: 4 != 3 ✗
  │                                    ├─ ROLLBACK
  │                                    └─ raises OptimisticConcurrencyError
```

**What the losing agent receives:** `OptimisticConcurrencyError("Expected version 3, got 4 for stream loan-X")`.

**What it must do next:**
1. Reload the stream: `load_stream(loan-X)` — this returns 4 events, version=4.
2. Inspect the new event at position 4 — it is the winning agent's `CreditAnalysisCompleted`.
3. Decide whether its own analysis is still relevant given the new state. If the winning agent's decision supersedes it (e.g. same risk tier), it may discard its result. If its analysis differs materially, it may append a second `CreditAnalysisCompleted` at `expected_version=4` — but only if the business rules (model version locking) permit it.

---

## 4. Projection Lag and Its Consequences

**Question:** The `LoanApplication` projection lags 200ms. A loan officer queries "available credit limit" immediately after a disbursement event commits. They see the old limit. What does your system do, and how do you communicate this to the UI?

**What the system does:**

The `ApplicationSummary` projection is eventually consistent. When the loan officer queries `ledger://applications/{id}`, the response comes from the projection table — which has not yet processed the `LoanDisbursed` event. The response will show the pre-disbursement credit limit.

The system does not block the query waiting for the projection to catch up. That would defeat the purpose of CQRS.

**How to communicate this to the UI:**

Every projection response includes two fields:
- `as_of_global_position: int` — the last global event position this projection has processed
- `as_of_timestamp: datetime` — the wall-clock time of that position

The UI uses these to display a staleness indicator: *"Data current as of [timestamp]. Refresh for latest."*

For the credit limit specifically — a field where stale data has financial consequences — the UI should:
1. Show the current projection value with the staleness timestamp.
2. Offer a "refresh" action that re-queries after a short delay (e.g. 500ms), giving the projection time to catch up.
3. For automated systems (not human UIs), implement a read-your-writes pattern: after appending a `LoanDisbursed` event, poll `stream_version()` until the projection's `as_of_global_position` has advanced past the event's `global_position`.

**SLO commitment:** The `ApplicationSummary` projection must maintain lag under 500ms in normal operation. The `ComplianceAuditView` may lag up to 2 seconds.

---

## 5. The Upcasting Scenario

**Question:** `CreditDecisionMade` was defined in 2024 with `{application_id, decision, reason}`. In 2026 it needs `{application_id, decision, reason, model_version, confidence_score, regulatory_basis}`. Write the upcaster. What is your inference strategy for historical events that predate `model_version`?

**Upcaster implementation:**

```python
from src.upcasting.registry import registry

@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        # Inference: model_version is inferred from recorded_at timestamp.
        # All events before 2026-01-01 used the legacy scoring model.
        # This is a best-effort inference — not fabrication, because we
        # document the inference rule and its uncertainty.
        "model_version": "legacy-pre-2026",

        # confidence_score is genuinely unknown for v1 events.
        # We use null rather than fabricating a value, because:
        # - Any fabricated score would be indistinguishable from a real score
        # - Downstream systems (e.g. AgentPerformanceLedger) would compute
        #   incorrect averages if we inject a synthetic value
        # - Regulators examining historical decisions must see that confidence
        #   was not recorded, not a plausible-looking fabricated number
        "confidence_score": None,

        # regulatory_basis: infer from the regulation set version active
        # at the recorded_at date. This requires a lookup table of
        # regulation set versions by effective date.
        "regulatory_basis": _infer_regulatory_basis(payload.get("recorded_at")),
    }

def _infer_regulatory_basis(recorded_at) -> str:
    """Return the regulation set version active at recorded_at."""
    if recorded_at is None or recorded_at < "2025-03-01":
        return "REG-SET-2024-Q4"
    return "REG-SET-2025-Q1"
```

**Inference strategy for `model_version`:**

Use the `recorded_at` timestamp as a proxy. All events recorded before the 2026 model deployment date are assigned `"legacy-pre-2026"`. This is an inference, not a fabrication — it is documented, auditable, and carries a known uncertainty (events near the deployment boundary may be misclassified by ±1 day).

**Why `null` over inference for `confidence_score`:**

`confidence_score` was never computed for v1 events — the scoring model did not produce it. Inferring a value (e.g. the historical average) would:
1. Make historical events appear more precise than they were.
2. Corrupt aggregate metrics in the `AgentPerformanceLedger` projection.
3. Create a compliance risk: a regulator examining a historical decision would see a confidence score that was never actually computed.

`null` is the honest representation of "this field did not exist when this event was recorded."

---

## 6. The Marten Async Daemon Parallel

**Question:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in Python. What coordination primitive do you use, and what failure mode does it guard against?

**Marten's pattern:** Multiple application nodes each run a projection daemon. Marten uses PostgreSQL advisory locks to elect a single leader per projection. Only the leader processes events; followers wait. If the leader dies, a follower acquires the lock and takes over within seconds.

**Python equivalent:**

```python
# Coordination primitive: PostgreSQL advisory lock
# Each projection daemon attempts to acquire pg_try_advisory_lock(projection_id_hash)
# Only one node holds the lock at a time.

async def run_projection_with_leader_election(
    pool: asyncpg.Pool,
    projection: Projection,
    poll_interval_ms: int = 100,
) -> None:
    projection_lock_id = hash(projection.name) % (2**31)  # pg advisory lock key

    async with pool.acquire() as conn:
        acquired = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", projection_lock_id
        )
        if not acquired:
            return  # another node is the leader; this node stands by

        try:
            while True:
                await _process_batch(conn, projection)
                await asyncio.sleep(poll_interval_ms / 1000)
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", projection_lock_id
            )
```

**What failure mode this guards against:**

**Duplicate projection processing.** Without leader election, two daemon nodes processing the same events would produce duplicate writes to the projection table — double-counting metrics in `AgentPerformanceLedger`, duplicate rows in `ApplicationSummary`, and incorrect compliance states in `ComplianceAuditView`.

The advisory lock ensures exactly one node processes each batch. If the leader crashes (connection drops), PostgreSQL automatically releases the advisory lock, and a standby node acquires it within one poll interval — providing automatic failover without manual intervention.

**Additional coordination:** Each projection maintains its own checkpoint in `projection_checkpoints`. Even if a node crashes mid-batch, the next leader resumes from the last committed checkpoint, not from the beginning. This provides at-least-once processing semantics with idempotent projection handlers.

---

## 7. EventStoreDB Comparison (Stack Orientation)

| Concept | This PostgreSQL Implementation | EventStoreDB Equivalent |
|---|---|---|
| Stream | `event_streams` row + `events` rows with matching `stream_id` | Native stream (e.g. `loan-{id}`) |
| `load_stream()` | `SELECT ... WHERE stream_id = $1 ORDER BY stream_position` | `ReadStreamAsync()` |
| `load_all()` | `SELECT ... WHERE global_position > $last ORDER BY global_position` | `$all` stream subscription |
| `ProjectionDaemon` | Custom asyncio polling loop with checkpoint table | Built-in persistent subscriptions with automatic checkpointing |
| Optimistic concurrency | `SELECT FOR UPDATE` + version check in transaction | Native `expectedVersion` parameter on `AppendToStreamAsync` |
| Outbox | Custom `outbox` table written in same transaction | Not built-in — requires custom implementation |

**What EventStoreDB gives you that this implementation must work harder to achieve:**
- **Persistent subscriptions** with automatic checkpoint management, competing consumer groups, and built-in retry logic — replacing the entire `ProjectionDaemon` implementation.
- **Native gRPC streaming** for real-time event delivery — replacing the polling loop with a push model.
- **Built-in stream metadata** (max count, max age, ACLs) — replacing the `archive_stream` soft-delete approach.
- **$all stream** with server-side filtering by event type — replacing the `load_all(event_types=[...])` client-side filter.

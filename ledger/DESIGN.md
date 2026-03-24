# DESIGN.md — The Ledger: Architectural Decisions and Tradeoffs

---

## 1. Aggregate Boundary Justification

### Why ComplianceRecord is a separate aggregate from LoanApplication

`LoanApplication` owns the loan lifecycle state machine (Submitted → FinalApproved/FinalDeclined).
`ComplianceRecord` owns the regulatory check accumulation (required checks, passed checks, clearance).
They share an `application_id` but write to different streams: `loan-{id}` and `compliance-{id}`.

**What would couple if you merged them:**

The compliance check flow is multi-step and non-atomic. A single application may require 3–5 rule
evaluations (`AML`, `KYC`, `OFAC`, `CREDIT_HISTORY`, `FRAUD_SCREEN`), each arriving as a separate
command from a separate agent or regulatory service. If these were appended to the `loan-{id}` stream,
every compliance rule evaluation would contend on the same stream version as the loan lifecycle
commands.

Concretely: `handle_compliance_check` in `handlers.py` calls `ComplianceRecordAggregate.load()` and
then `record_rule_passed()` or `record_rule_failed()`. This is a load → validate → append cycle on
`compliance-{id}`. If compliance events lived on `loan-{id}`, this same cycle would compete with
`handle_credit_analysis_completed`, `handle_generate_decision`, and `handle_human_review_completed`
— all of which also load and append to the loan stream.

**The specific failure mode under concurrent writes:**

Consider 3 compliance agents running in parallel, each evaluating one rule for the same application.
All three call `store.append("loan-{id}", ..., expected_version=N)` simultaneously. The `SELECT FOR
UPDATE` in `EventStore.append()` serialises them, but only one succeeds at version N. The other two
receive `OptimisticConcurrencyError` and must reload and retry. With 3 agents, the expected number
of retries is 2 (the second agent retries once, the third retries twice in the worst case). With 5
compliance rules, worst-case retries = 4 per application.

Now add the loan lifecycle commands running concurrently. A `handle_credit_analysis_completed` call
that loaded the stream at version N will fail if any compliance event was appended in the interim.
The loan state machine and the compliance accumulation would be fighting over the same version
counter for unrelated reasons. A compliance rule evaluation — which has no business relationship to
the credit analysis — would cause the credit analysis handler to retry.

**With separate streams:** `compliance-{id}` and `loan-{id}` have independent version counters and
independent `SELECT FOR UPDATE` locks. Concurrent compliance rule evaluations contend only with each
other, not with loan lifecycle commands. The loan stream advances only when the loan state machine
advances. This is the correct isolation boundary: one aggregate, one invariant set, one lock.

**The cross-stream dependency (Business Rule 5)** — `ApplicationApproved` cannot be appended unless
all compliance checks are passed — is enforced at the command handler level in
`handle_approve_application`, which loads both aggregates and calls
`compliance.assert_all_checks_passed()` before calling `app.approve()`. This is a read-time
consistency check, not a write-time lock, which is the correct pattern for cross-aggregate
invariants in event sourcing.

---

## 2. Projection Strategy

### ApplicationSummaryProjection

**Inline vs. Async:** Async (daemon-driven via `ProjectionDaemon`).

The application summary is a read-optimised denormalised view. It does not need to be consistent
with the write side within the same request — callers querying application state can tolerate
sub-second staleness. Running it inline (synchronously in the command handler) would add a DB write
to every command's critical path and couple the write side to the read model's schema.

**SLO:** Lag < 500ms under normal load. The daemon polls every 100ms with a batch size of 100
events. At 100 concurrent applications each generating ~10 events, the daemon processes 1000 events
per second in steady state, well within the 500ms SLO.

**Rebuild:** `TRUNCATE TABLE application_summary` + checkpoint reset to 0. The daemon replays all
events from position 0 on the next poll. Live reads continue against the empty table during rebuild
— no downtime, but stale reads until caught up.

---

### AgentPerformanceLedgerProjection

**Inline vs. Async:** Async (daemon-driven).

Performance metrics are aggregated counters (`analyses_completed`, `total_confidence_score`,
`approve_count`, etc.). They are queried by the MCP resource `ledger://agents/{id}/performance` for
monitoring and drift detection, not for real-time decisions. Async is correct: a 500ms lag on a
monitoring dashboard is acceptable; coupling the credit analysis write path to a metrics update is
not.

**SLO:** Lag < 500ms. Same reasoning as ApplicationSummary.

**Deduplication:** The handler guards `if not event.stream_id.startswith("agent-")` for
`CreditAnalysisCompleted` events, because the same event is written to both the agent session stream
and the loan stream by `handle_credit_analysis_completed`. Without this guard, every analysis would
be counted twice.

---

### ComplianceAuditViewProjection

**Inline vs. Async:** Async (daemon-driven).

Compliance audit data is queried by regulators and compliance officers, not by the real-time
decision path. The temporal query (`get_compliance_at`) is a read-time operation that replays from
snapshots. Async is correct.

**SLO:** Lag < 2000ms. Compliance queries are not latency-sensitive in the same way as application
state queries. The higher SLO budget allows the daemon to batch more events per poll cycle, reducing
DB round-trips.

**Snapshot strategy: event-count trigger**

A snapshot is taken every `_SNAPSHOT_EVERY_N = 10` compliance events per application. This is an
event-count trigger, not a time trigger or manual trigger.

Justification for event-count over time-trigger: compliance events arrive in bursts (all rules for
one application evaluated within seconds), then silence. A time-based trigger (e.g. every 60
seconds) would either snapshot too frequently during quiet periods (wasting storage) or too
infrequently during bursts (defeating the purpose). An event-count trigger fires exactly when the
replay cost justifies the snapshot cost: after 10 events, worst-case replay from the snapshot is 9
events.

Justification for event-count over manual: manual snapshots require operational intervention and
create operational risk (forgotten snapshots, inconsistent snapshot timing across applications).
Automatic triggers are more reliable.

**Snapshot invalidation logic:**

Snapshots are immutable once written. They are never updated in place. If a projection rebuild is
triggered (via `rebuild(conn)`), `TRUNCATE TABLE compliance_audit_snapshots` removes all snapshots
and `TRUNCATE TABLE compliance_audit_events` removes the audit log. The daemon replays all events
from position 0, rebuilding both tables from scratch. This is the only invalidation path.

There is no partial invalidation. If a single application's compliance data is corrupted, the only
correct action is a full rebuild, because the `_event_counts` in-memory counter would also be
inconsistent. Partial invalidation would require resetting the counter for that application and
re-scanning its events — complexity not justified by the use case.

**`get_compliance_at` temporal query path:**

1. Find the latest snapshot with `snapshot_at <= timestamp`.
2. If found: deserialise the snapshot state, then replay only `compliance_audit_events` with
   `global_position > snapshot.last_global_position AND recorded_at <= timestamp`.
3. If not found: full replay of all `compliance_audit_events` with `recorded_at <= timestamp`.

The worst-case replay cost without any snapshot is O(N) where N is the total compliance events for
that application. With snapshots every 10 events, worst-case is O(10) — constant regardless of
application age.

---

## 3. Concurrency Analysis

### Setup

Peak load: 100 concurrent applications, 4 agents each.

Each application follows this write sequence on `loan-{id}`:
1. `ApplicationSubmitted` (version 1)
2. `CreditAnalysisRequested` (version 2)
3. `CreditAnalysisCompleted` (version 3)
4. `ComplianceCheckRequested` (version 4)
5. `DecisionGenerated` (version 5)
6. `HumanReviewCompleted` (version 6)
7. `ApplicationApproved` or `ApplicationDeclined` (version 7)

Each application also has a `compliance-{id}` stream with 3–5 rule evaluations, and 4 agent session
streams (`agent-{id}-{session}`).

### OptimisticConcurrencyError rate on loan-{id} streams

The `loan-{id}` stream is written by sequential lifecycle commands. In the normal flow, each command
is issued by a single orchestrator after the previous one completes. There is no inherent concurrency
on the loan stream itself — the state machine is strictly sequential.

The only realistic race on `loan-{id}` is the double-decision scenario tested in
`test_concurrency.py`: two agents simultaneously attempt `CreditAnalysisCompleted` at the same
version. This is the scenario the `SELECT FOR UPDATE` lock is designed to prevent.

**Expected OptimisticConcurrencyErrors per minute on loan-{id} streams:**

Under normal orchestration (one agent assigned per application), the expected rate is **0 per
minute**. The orchestrator assigns one agent per application; there is no legitimate reason for two
agents to race on the same loan stream.

Under pathological conditions (misconfigured orchestrator, retry storm, duplicate command delivery):
- 100 applications × 1 race per application = 100 potential races
- Each race produces exactly 1 `OptimisticConcurrencyError` (the loser)
- At 100 concurrent applications with a 1-second processing window: **~100 errors/minute**

The `compliance-{id}` streams have higher inherent concurrency: 3–5 rule evaluations per
application, potentially issued in parallel. At 100 applications × 4 concurrent rule evaluations:
- Expected conflicts per application: 3 (4 agents, 1 succeeds, 3 retry)
- Total: 100 × 3 = **300 OptimisticConcurrencyErrors/minute** on compliance streams

### Retry strategy

The current implementation does **not** implement automatic retry in the command handlers. An
`OptimisticConcurrencyError` propagates to the caller. This is a deliberate choice: the handler
does not know whether the conflict was caused by a legitimate concurrent write (in which case retry
is correct) or a programming error (in which case retry would mask the bug).

The recommended retry strategy for callers:

```
max_retries = 3
backoff_base_ms = 50
for attempt in range(max_retries):
    try:
        return await handle_credit_analysis_completed(cmd, store)
    except OptimisticConcurrencyError:
        if attempt == max_retries - 1:
            raise
        await asyncio.sleep(backoff_base_ms * (2 ** attempt) / 1000)
```

**Maximum retry budget:** 3 attempts with exponential backoff (50ms, 100ms, 200ms). Total maximum
wait: 350ms before returning failure to the caller. This is within the 500ms SLO for the
ApplicationSummary projection and leaves headroom for the DB round-trips.

After 3 failed attempts, the error is returned to the caller. The caller (MCP tool or API handler)
is responsible for surfacing the conflict to the user or re-queuing the command. Silent infinite
retry is not acceptable: it would mask a stuck stream and delay detection of a real bug.

---

## 4. Upcasting Inference Decisions

### CreditAnalysisCompleted v1 → v2

**Field: `model_version`**

Inferred as the sentinel string `"legacy-pre-2026"`.

Likely error rate: ~0%. Any event stored with `event_version=1` genuinely predates the v2 schema
introduction. There is no scenario where a v1 event was produced by a system that knew the model
version but failed to record it — the field simply did not exist in the v1 schema.

Downstream consequence of incorrect inference: model drift analysis in `AgentPerformanceLedger`
would bucket all legacy events under `"legacy-pre-2026"`. If this sentinel were confused with a real
model version, drift metrics would be polluted. The sentinel is chosen to be obviously non-real
(contains a hyphen and a year), making accidental confusion unlikely.

When to choose null instead: if the model version were a required field in downstream business logic
(e.g. a compliance rule that rejects analyses from unknown model versions), null would be safer than
a sentinel — it would cause an explicit failure rather than a silent pass with a fake version. In
this codebase, `model_version` is used for grouping and display only, so the sentinel is acceptable.

---

**Field: `confidence_score`**

Set to `None`. Not inferred.

Likely error rate: 0% — there is no correct value to infer. The v1 schema did not compute or store
confidence scores. Any numeric value would be fabricated.

Downstream consequence of incorrect inference: if we inferred `0.5` (midpoint), the
`AgentPerformanceLedger` `avg_confidence_score` would be polluted with invented data. More
critically, the confidence floor rule (`score < 0.6 → REFER`) could be incorrectly applied during
what-if analysis on historical events, producing counterfactual outcomes that never could have
occurred under the real v1 system.

`None` is the honest answer. Downstream consumers (`AgentPerformanceLedger.handle` guards
`confidence = p.get("confidence_score", 0.0) or 0.0`) must handle `None` explicitly, which forces
acknowledgement of the data gap rather than silent consumption of a fabricated value.

**When to choose null over inference:** always, when the field was genuinely not computed by the
producing system. Null is a data contract: it tells the consumer "this information does not exist."
An inferred value is a lie that looks like data.

---

**Field: `regulatory_basis`**

Inferred as the sentinel `"legacy-regulatory-framework"`.

Likely error rate: ~0% for the same reason as `model_version` — v1 events predate the field.

Downstream consequence: compliance audit queries that filter by `regulatory_basis` would group all
legacy events together. This is accurate: they were all evaluated under the same pre-v2 regulatory
framework.

Why not null: `regulatory_basis` is a required audit field. A null value would break compliance
queries that expect it to be present (e.g. `WHERE regulatory_basis = $1` would exclude null rows
silently). The sentinel is preferable to null here because it is queryable and clearly non-real.

---

### DecisionGenerated v1 → v2

**Field: `model_versions` dict**

Inferred as `{session_id: "unknown"}` for each session in `contributing_agent_sessions`.

Likely error rate: 100% — every inferred value is wrong. The actual model version for each session
is stored in that session's `AgentContextLoaded` event, but upcasters are pure functions with no
store access. We cannot look it up at upcast time.

Downstream consequence: any system that uses `model_versions` to make decisions (e.g. "reject
decisions from model version X") would treat all historical decisions as coming from `"unknown"`,
which is neither approved nor rejected. This is a safe failure mode: the decision is not silently
accepted or rejected on false grounds.

The `"unknown"` sentinel is preferable to null for the same reason as `regulatory_basis`: the dict
key exists and is queryable. A null value for the entire dict would break code that iterates
`model_versions.items()`.

**When to choose null over inference:** if the downstream consumer would make a binary
approve/reject decision based on the field value, null is safer than a sentinel — it forces an
explicit null-check rather than a silent pass with `"unknown"`. In this codebase, `model_versions`
is used for audit display and drift analysis, not for binary decisions, so `"unknown"` is acceptable.

**The correct long-term fix:** store `model_version` in the `DecisionGenerated` payload at write
time (v2 schema), eliminating the need for inference entirely. The upcaster is a migration aid for
historical data, not a permanent solution.

---

## 5. EventStoreDB Comparison

### Streams → EventStoreDB stream IDs

Our `stream_id` TEXT column (`loan-{id}`, `agent-{id}-{session}`, `compliance-{id}`) maps directly
to EventStoreDB stream IDs. EventStoreDB uses the same human-readable string convention. The
`aggregate_type` column in our `event_streams` table maps to EventStoreDB's stream category
convention: EventStoreDB automatically creates a `$ce-LoanApplication` category stream that
aggregates all streams prefixed `LoanApplication-`. We implement this manually via the
`event_types` filter in `load_all()` and the `aggregate_type` column.

### load_all() → EventStoreDB $all stream subscription

Our `load_all()` is a polling async generator that fetches events in batches ordered by
`global_position`. It is functionally equivalent to EventStoreDB's `$all` stream, which is the
global ordered log of all events across all streams.

The key difference: EventStoreDB's `$all` subscription is a **push** model — the server notifies
subscribers when new events arrive. Our `load_all()` is a **pull** model — the `ProjectionDaemon`
polls on a 100ms interval. This means:

- Our implementation has up to 100ms additional latency per batch even when events are available.
- EventStoreDB push subscriptions have near-zero additional latency.
- Our polling adds constant DB load (one SELECT per 100ms per daemon instance) even when there are
  no new events. EventStoreDB push subscriptions are idle when there are no events.

We mitigate this with the `if processed == 0: await asyncio.sleep(poll_interval_ms / 1000)` pattern
in `ProjectionDaemon.run_forever()`, but the fundamental polling overhead remains.

### ProjectionDaemon → EventStoreDB persistent subscriptions

Our `ProjectionDaemon` with per-projection checkpoints in `projection_checkpoints` maps to
EventStoreDB's **persistent subscriptions**. Both:
- Track the last processed position per named subscription/projection.
- Resume from the checkpoint after restart.
- Support independent advancement (a slow projection does not block a fast one).
- Support rebuild (reset checkpoint to 0, replay from the beginning).

**What EventStoreDB gives you that our implementation must work harder to achieve:**

1. **Push-based delivery.** EventStoreDB notifies subscribers immediately when events are appended.
   We poll. This is the most significant operational difference.

2. **Built-in competing consumers.** EventStoreDB persistent subscriptions support multiple
   consumer instances with load balancing and exactly-once delivery semantics. Our daemon is a
   single-instance polling loop. To scale horizontally, we would need to add distributed locking
   (e.g. advisory locks in PostgreSQL) to prevent two daemon instances from processing the same
   event simultaneously.

3. **Server-side projections.** EventStoreDB has a built-in JavaScript projection engine that runs
   server-side, with access to the full event log without network round-trips. Our projections run
   client-side and pay a network round-trip per batch.

4. **Stream category subscriptions.** EventStoreDB's `$ce-LoanApplication` category stream is
   maintained automatically. We implement category filtering manually via the `event_types` list
   and `aggregate_type` column.

5. **Optimistic concurrency built into the protocol.** EventStoreDB's append API accepts
   `expectedRevision` natively. Our implementation replicates this with `SELECT FOR UPDATE` +
   version check, which is correct but requires careful transaction management.

6. **No polling overhead.** EventStoreDB's internal architecture uses an in-memory queue and
   file-based storage optimised for sequential writes. Our PostgreSQL implementation pays B-tree
   index maintenance costs on every INSERT.

---

## 6. What I Would Do Differently

**The single most significant architectural decision I would reconsider: the dual-write pattern in `handle_credit_analysis_completed`.**

Currently, `handle_credit_analysis_completed` writes `CreditAnalysisCompleted` to **two streams**:
the agent session stream (`agent-{id}-{session}`) and the loan application stream (`loan-{id}`).
This is implemented as two sequential `store.append()` calls in the handler.

```python
await agent.record_credit_analysis(store, ...)   # writes to agent-{id}-{session}
await app.record_credit_analysis_completed(store, ...)  # writes to loan-{id}
```

This is not atomic. If the first append succeeds and the second fails (network error, concurrency
conflict on the loan stream), the agent session stream has a `CreditAnalysisCompleted` event that
the loan stream does not. The system is now in an inconsistent state: the agent believes it
completed an analysis; the loan application does not know about it.

The `AgentPerformanceLedger` projection guards against double-counting with
`if not event.stream_id.startswith("agent-")`, but this is a symptom of the underlying problem:
the same domain fact is stored twice, in two places, with no atomicity guarantee between them.

**What I would do instead:**

Write `CreditAnalysisCompleted` to the agent session stream only. The loan application stream
subscribes to agent session events via a process manager (saga) that listens on the `$all` stream
for `CreditAnalysisCompleted` events and issues a `RecordCreditAnalysisOnLoan` command to the loan
stream. This makes the cross-stream write explicit, observable, and retryable.

The tradeoff: this introduces eventual consistency between the agent session stream and the loan
stream. The loan stream would lag the agent session stream by one process manager cycle. For the
current use case (the loan state machine advances after the analysis is recorded), this lag is
acceptable — the process manager would complete within milliseconds under normal conditions.

The benefit: the agent session stream is the single source of truth for what the agent did. The
loan stream reflects what the loan application knows. These are different facts, and they should
have different write paths. The current dual-write conflates them into a single handler, which
creates a hidden atomicity requirement that PostgreSQL transactions cannot satisfy across two
separate `store.append()` calls.

This is the decision I would reconsider because it is the one most likely to cause a production
incident: a partial write that leaves the system in a state that is internally consistent within
each aggregate but inconsistent across aggregates, with no automatic recovery path.

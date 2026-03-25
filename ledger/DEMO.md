# Video Demo Walkthrough — The Ledger

## Prerequisites

Postgres running and the `ledger_test` database accessible via the connection string in `.env`:

```
DATABASE_URL=postgresql://shuaib@/ledger_test?host=/var/run/postgresql
```

The demo script drops and recreates all tables on every run, so no manual DB setup is needed.

---

## Running the demo

From the `ledger/` directory:

```bash
uv run python demo.py
```

The script runs all 6 steps sequentially and exits. Total runtime is under 5 seconds. Each step prints coloured output — green `✓` for pass, yellow `⚠` for expected warnings (e.g. the concurrency error), red `✗` for failures.

---

## What each step does and what to point at

---

### Step 1 — Complete Decision History (mins 1–2)

**What it does:**
Runs the full loan application lifecycle end-to-end for a single application ID, timed from first event to last:

1. `ApplicationSubmitted`
2. `AgentContextLoaded` (Gas Town — agent declares context before any decision)
3. `CreditAnalysisRequested`
4. `CreditAnalysisCompleted` — risk_tier=MEDIUM, confidence=0.87
5. `FraudScreeningCompleted` — fraud_score=0.12
6. `ComplianceRulePassed` × 2 (KYC-001, AML-002)
7. `DecisionGenerated` — recommendation=APPROVE
8. `HumanReviewCompleted` — final_decision=APPROVE

**What to point at in the output:**

- **Full event stream** — every event printed with its `stream_position`, `global_position`, and `correlation_id`. All 6 loan-stream events share the same `corr=` prefix, proving they are causally linked.
- **Causal links** — the `correlation_id` chain printed as a single arrow chain: `ApplicationSubmitted → ... → HumanReviewCompleted`.
- **Compliance state** — `passed_checks=['KYC-001', 'AML-002']` read from the `ComplianceAuditView` projection.
- **Cryptographic integrity** — `events_verified=6`, `chain_valid=True`, `tamper_detected=False`, and the first 24 chars of the SHA-256 `integrity_hash`.
- **ApplicationSummary projection** — `state=ApprovedPendingHuman`, `decision=APPROVE`, `risk_tier=MEDIUM` read from the `application_summary` table via the daemon.
- **Timer** — `Total time: ~0.08s  SLA <60s: ✓ PASS`

---

### Step 2 — Concurrency Under Pressure (min 2)

**What it does:**
Seeds a stream at version 1, then fires two agents simultaneously with `asyncio.gather`. Both read version=1 before either commits. One wins; the other gets `OptimisticConcurrencyError`, reloads the stream, and retries successfully.

**What to point at in the output:**

- `Agent Alpha: append SUCCEEDED → stream now at version 2`
- `Agent Beta: OptimisticConcurrencyError  expected=1  actual=2` — the yellow `⚠` line. This is the expected collision.
- `Agent Beta: retry SUCCEEDED after reload → version 3` — the agent recovered autonomously.
- `Final stream version: 3  (both events recorded, no data lost)` — neither agent's event was dropped.

The key point: the DB-level `SELECT ... FOR UPDATE` inside `EventStore.append` serialises concurrent writers on the same stream. Only one transaction holds the lock at a time. The loser gets a typed error with `suggested_action="reload_stream_and_retry"` so it can recover without human intervention.

---

### Step 3 — Temporal Compliance Query (min 2–3)

**What it does:**
Queries `ComplianceAuditViewProjection.get_compliance_at()` twice for the same application — once at `2020-01-01` (before any events existed) and once at `NOW` (after KYC-001 and AML-002 were passed in Step 1).

**What to point at in the output:**

- `Compliance at 2020-01-01` → `passed_checks=[]` — the application did not exist yet; the projection correctly returns an empty state.
- `Compliance at NOW` → `passed_checks=['KYC-001', 'AML-002']` — the current state after both rules passed.
- These are two distinct states from the same projection query, demonstrating point-in-time time-travel without touching the immutable event log.

The implementation: `get_compliance_at(app_id, timestamp, conn)` finds the latest snapshot at or before the timestamp, then replays only the `compliance_audit_events` rows between that snapshot and the timestamp. No event log replay needed for the common case.

---

### Step 4 — Upcasting & Immutability (min 3–4)

**What it does:**
Writes a `CreditAnalysisCompleted` event with `event_version=1` — the old schema that has no `model_version`, `confidence_score`, or `regulatory_basis` fields. Then reads it back via `load_stream()` and queries the raw DB row directly.

**What to point at in the output:**

- **In-memory (after upcasting):**
  - `event_version : 2` — the upcaster chain advanced the version
  - `model_version : 'legacy-pre-2026'` — sentinel inferred, not fabricated
  - `confidence_score: None` — honest unknown, not a made-up 0.5
  - `regulatory_basis: 'legacy-regulatory-framework'`

- **Raw DB row (immutable):**
  - `event_version : 1` — the stored row is unchanged
  - `model_version in payload: False` — the v1 payload has no such field

The upcaster runs transparently inside `_apply_upcasts()` on every `load_stream()` call. The stored payload is never touched. This is the core immutability guarantee: the event log is a permanent record; schema evolution happens at read time only.

---

### Step 5 — Gas Town Recovery (min 4–5)

**What it does:**
Starts an agent session, submits an application, requests credit analysis, and records a `CreditAnalysisCompleted` event — then simulates a process crash by simply not writing any further events. Calls `reconstruct_agent_context()` to replay the session stream and rebuild the agent's context.

**What to point at in the output:**

- `>>> SIMULATING CRASH — process killed here <<<` — the point of interruption.
- **Reconstructed context:**
  - `session_health_status : NEEDS_RECONCILIATION` — the agent committed a `CreditAnalysisCompleted` but no `FraudScreeningCompleted` followed. The system detected the partial state.
  - `last_event_position : 2` — the agent knows exactly where to resume.
  - `model_version : apex-v2.1` — recovered from the `AgentContextLoaded` event.
  - `pending_work` — a human-readable description of the unresolved work item, ready to inject into an LLM context window.
- **Context text** — the first 400 chars of the reconstructed prose summary, showing the verbatim last events and the `⚠ PENDING WORK` warning.

The Gas Town pattern: every agent action is written to the event store *before* execution. On restart, `reconstruct_agent_context()` replays the stream, identifies partial decisions, and returns a token-efficient context string the agent can use to resume without re-doing completed work.

---

### Step 6 — What-If Counterfactual (min 5–6)

**What it does:**
Takes the application from Step 1 (real outcome: APPROVE, risk_tier=MEDIUM) and asks: *what would have happened if the credit analysis had returned risk_tier=HIGH instead?*

Constructs a counterfactual `CreditAnalysisCompleted` event with `risk_tier=HIGH`, passes it to `run_what_if()`, which:
1. Splits the real event stream at the branch point.
2. Identifies `DecisionGenerated` and `HumanReviewCompleted` as causally dependent on the branched event — they are skipped in the counterfactual timeline.
3. Replays both timelines through the `ApplicationSummaryProjection` inside a rolled-back transaction.
4. Returns both outcomes and the divergence analysis.

**What to point at in the output:**

- `Branch point: CreditAnalysisCompleted`
- `Skipped real events: ['DecisionGenerated', 'HumanReviewCompleted']` — causally dependent events excluded from the counterfactual.
- `Divergence events: ['CreditAnalysisCompleted', 'DecisionGenerated', 'HumanReviewCompleted']` — events that differ between timelines.
- **Real timeline:** `risk_tier=MEDIUM  decision=APPROVE  state=ApprovedPendingHuman`
- **Counterfactual timeline:** `risk_tier=HIGH  decision=APPROVE  state=ComplianceReview` — the HIGH risk tier stalled the pipeline at ComplianceReview; the downstream decision was never reached.
- `Real store verified unchanged` — the counterfactual ran entirely inside a rolled-back transaction. The production event log was never written to.

---

## Expected full output (clean run)

```
════════════════════════════════════════════════════════════
  THE LEDGER — Video Demo
════════════════════════════════════════════════════════════

────────────────────────────────────────────────────────────
  STEP 1 — Complete Decision History (end-to-end, timed)
────────────────────────────────────────────────────────────
  ✓ ApplicationSubmitted  stream=loan-demo-xxxxxxxx
  ✓ AgentContextLoaded    stream=agent-agent-apex-1-xxxxxxxx
  ✓ CreditAnalysisRequested
  ✓ CreditAnalysisCompleted  risk_tier=MEDIUM  confidence=0.87
  ✓ FraudScreeningCompleted  fraud_score=0.12
  ✓ ComplianceRulePassed  rule=KYC-001
  ✓ ComplianceRulePassed  rule=AML-002
  ✓ DecisionGenerated  recommendation=APPROVE
  ✓ HumanReviewCompleted  final_decision=APPROVE

  Full event stream for loan-demo-xxxxxxxx:
    [01] gpos=0001  ApplicationSubmitted                corr=xxxxxxxx
    [02] gpos=0003  CreditAnalysisRequested             corr=xxxxxxxx
    [03] gpos=0005  CreditAnalysisCompleted             corr=xxxxxxxx
    [04] gpos=0010  ComplianceCheckRequested            corr=xxxxxxxx
    [05] gpos=0011  DecisionGenerated                   corr=xxxxxxxx
    [06] gpos=0012  HumanReviewCompleted                corr=xxxxxxxx

  Causal links (correlation_id):
    corr=xxxxxxxx  →  ApplicationSubmitted → ... → HumanReviewCompleted

  Compliance state:
    passed_checks=['KYC-001', 'AML-002']  clearance=False

  Cryptographic integrity:
    events_verified=6  chain_valid=True
    integrity_hash=<24 hex chars>...
    tamper_detected=False

  ApplicationSummary projection:
    state=ApprovedPendingHuman  decision=APPROVE  risk_tier=MEDIUM  fraud_score=0.12

  ⏱  Total time: ~0.08s  SLA <60s: ✓ PASS

────────────────────────────────────────────────────────────
  STEP 2 — Concurrency Under Pressure (OptimisticConcurrencyError)
────────────────────────────────────────────────────────────
  ✓ Stream seeded at version 1
  ✓ Agent Alpha: append SUCCEEDED → stream now at version 2
  ⚠ Agent Beta: OptimisticConcurrencyError  expected=1  actual=2
  ✓ Agent Beta: retry SUCCEEDED after reload → version 3
  ✓ Final stream version: 3  (both events recorded, no data lost)

────────────────────────────────────────────────────────────
  STEP 3 — Temporal Compliance Query (as_of timestamp)
────────────────────────────────────────────────────────────
  Compliance at 2020-01-01:  passed_checks=[]  clearance=False
  Compliance at NOW:         passed_checks=['KYC-001', 'AML-002']  clearance=False
  ✓ Temporal query returns distinct states — time-travel verified

────────────────────────────────────────────────────────────
  STEP 4 — Upcasting & Immutability
────────────────────────────────────────────────────────────
  ✓ Stored v1 CreditAnalysisCompleted (no model_version field)
  Event as returned by load_stream:
    event_version : 2  (was 1, now 2)
    model_version : 'legacy-pre-2026'
    confidence_score: None
    regulatory_basis: 'legacy-regulatory-framework'
  ✓ Upcasted event arrives as v2 in memory
  Raw DB row:
    event_version : 1  (still 1 in DB)
    model_version in payload: False
  ✓ Stored payload is unchanged — immutability preserved

────────────────────────────────────────────────────────────
  STEP 5 — Gas Town Recovery
────────────────────────────────────────────────────────────
  ✓ Agent appended 3 events
  → >>> SIMULATING CRASH — process killed here <<<
  Reconstructed context:
    session_health_status : NEEDS_RECONCILIATION
    last_event_position   : 2
    model_version         : apex-v2.1
    pending_work          : ["Reconcile partial CreditAnalysisCompleted ..."]
  ✓ Agent can resume with correct state — Gas Town recovery verified

────────────────────────────────────────────────────────────
  STEP 6 — What-If Counterfactual (HIGH risk tier)
────────────────────────────────────────────────────────────
  Branch point: CreditAnalysisCompleted
  Skipped real events: ['DecisionGenerated', 'HumanReviewCompleted']
  Counterfactual events injected: ['CreditAnalysisCompleted']
  Divergence events: ['CreditAnalysisCompleted', 'DecisionGenerated', 'HumanReviewCompleted']

  Real timeline:          risk_tier=MEDIUM  decision=APPROVE  state=ApprovedPendingHuman
  Counterfactual timeline: risk_tier=HIGH   decision=APPROVE  state=ComplianceReview
  ✓ Counterfactual replayed in rolled-back transaction — real store untouched
  ✓ Real store verified unchanged — immutability preserved

════════════════════════════════════════════════════════════
  ALL 6 STEPS COMPLETE
════════════════════════════════════════════════════════════
```

---

## Talking points per requirement

| Requirement | Where it appears |
|---|---|
| Full event stream | Step 1 — stream table with positions and global positions |
| All agent actions | Step 1 — each command handler prints on completion |
| Compliance checks | Step 1 — `ComplianceAuditViewProjection.get_current_compliance()` |
| Human review | Step 1 — `HumanReviewCompleted` event and projection state |
| Causal links | Step 1 — `correlation_id` chain printed as arrow chain |
| Cryptographic integrity | Step 1 — `run_integrity_check()` result with hash and `chain_valid` |
| Under 60 seconds | Step 1 — timer printed at the end |
| Two agents colliding | Step 2 — `asyncio.gather` race, one `OptimisticConcurrencyError`, one retry |
| Temporal compliance query | Step 3 — `get_compliance_at(app_id, past_ts)` vs `get_compliance_at(app_id, now_ts)` |
| Upcasting transparent | Step 4 — `event_version=2` in memory, `event_version=1` in DB |
| Immutability | Step 4 — raw DB row payload has no `model_version` field |
| Crash recovery | Step 5 — `reconstruct_agent_context()` returns `NEEDS_RECONCILIATION` with pending work |
| What-if counterfactual | Step 6 — `run_what_if()` with HIGH risk tier, divergence analysis, rolled-back transaction |

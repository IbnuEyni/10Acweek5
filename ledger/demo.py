"""
Video Demo Script — The Ledger (Week 5)
Covers all 6 steps in order. Run with: uv run python demo.py
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shuaib@/ledger_test?host=/var/run/postgresql")
SCHEMA = (Path(__file__).parent / "src" / "schema.sql").read_text()

# ── colour helpers ────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; C = "\033[96m"; R = "\033[91m"; B = "\033[1m"; X = "\033[0m"

def hdr(n: int, title: str) -> None:
    print(f"\n{B}{C}{'─'*60}{X}")
    print(f"{B}{C}  STEP {n} — {title}{X}")
    print(f"{B}{C}{'─'*60}{X}")

def ok(msg: str)  -> None: print(f"  {G}✓{X} {msg}")
def info(msg: str)-> None: print(f"  {C}→{X} {msg}")
def warn(msg: str)-> None: print(f"  {Y}⚠{X} {msg}")
def err(msg: str) -> None: print(f"  {R}✗{X} {msg}")


# ── DB bootstrap ──────────────────────────────────────────────────────────────

async def fresh_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
            DROP TABLE IF EXISTS
                outbox, events, projection_checkpoints, event_streams,
                compliance_audit_events, compliance_audit_snapshots,
                agent_performance_ledger, application_summary
            CASCADE
        """)
        await conn.execute(SCHEMA)
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Complete decision history end-to-end, timed
# ─────────────────────────────────────────────────────────────────────────────

async def step1_decision_history(pool: asyncpg.Pool) -> str:
    hdr(1, "Complete Decision History  [The Week Standard]")

    from src.event_store import EventStore
    from src.commands.handlers import (
        StartAgentSessionCommand, handle_start_agent_session,
        SubmitApplicationCommand, handle_submit_application,
        CreditAnalysisCompletedCommand, handle_credit_analysis_completed,
        FraudScreeningCompletedCommand, handle_fraud_screening_completed,
        ComplianceCheckCommand, handle_compliance_check,
        GenerateDecisionCommand, handle_generate_decision,
        HumanReviewCompletedCommand, handle_human_review_completed,
    )
    from src.integrity.audit_chain import run_integrity_check
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection
    from src.projections.daemon import ProjectionDaemon

    store = EventStore(pool)
    app_id = f"demo-{uuid.uuid4().hex[:8]}"
    agent_id = "agent-apex-1"
    session_id = uuid.uuid4().hex[:8]
    corr_id = str(uuid.uuid4())

    t0 = time.perf_counter()

    # 1. Submit application
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id, applicant_id="applicant-42",
            requested_amount_usd=25000, loan_purpose="home-improvement",
            correlation_id=corr_id,
        ), store)
    ok(f"ApplicationSubmitted  stream=loan-{app_id}")

    # 2. Start agent session (Gas Town)
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id, session_id=session_id,
            model_version="apex-v2.1", correlation_id=corr_id,
        ), store)
    ok(f"AgentContextLoaded    stream=agent-{agent_id}-{session_id}")

    # 3. Request credit analysis (advance state machine)
    from src.aggregates.loan_application import LoanApplicationAggregate
    app = await LoanApplicationAggregate.load(store, app_id)
    await app.request_credit_analysis(store, assigned_agent_id=agent_id, correlation_id=corr_id)
    ok("CreditAnalysisRequested")

    # 4. Credit analysis
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="apex-v2.1", confidence_score=0.87,
            risk_tier="MEDIUM", recommended_limit_usd=22000, duration_ms=312,
            correlation_id=corr_id,
        ), store)
    ok("CreditAnalysisCompleted  risk_tier=MEDIUM  confidence=0.87")

    # 5. Fraud screening
    await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            fraud_score=0.12, anomaly_flags=[], screening_model_version="fraud-v1.3",
            correlation_id=corr_id,
        ), store)
    ok("FraudScreeningCompleted  fraud_score=0.12")

    # 6. Compliance checks (loan state machine advanced inside handle_generate_decision)
    session_stream = f"agent-{agent_id}-{session_id}"
    await handle_compliance_check(
        ComplianceCheckCommand(
            application_id=app_id, rule_id="KYC-001", rule_version="v1",
            passed=True, regulation_set_version="v1.0",
            checks_required=["KYC-001", "AML-002"], correlation_id=corr_id,
        ), store)
    ok("ComplianceRulePassed  rule=KYC-001")

    await handle_compliance_check(
        ComplianceCheckCommand(
            application_id=app_id, rule_id="AML-002", rule_version="v1",
            passed=True, regulation_set_version="v1.0", correlation_id=corr_id,
        ), store)
    ok("ComplianceRulePassed  rule=AML-002")

    # Issue formal compliance clearance now that all required checks have passed
    from src.aggregates.compliance_record import ComplianceRecordAggregate
    compliance = await ComplianceRecordAggregate.load(store, app_id)
    await compliance.issue_clearance(store, correlation_id=corr_id)
    ok("ComplianceClearanceIssued  clearance=True")

    # 7. Generate decision — handler auto-advances AnalysisComplete → ComplianceReview
    await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=app_id, orchestrator_agent_id=agent_id,
            recommendation="APPROVE", confidence_score=0.87,
            contributing_agent_sessions=[session_stream],
            decision_basis_summary="Low fraud, medium risk, all checks passed.",
            correlation_id=corr_id,
        ), store)
    ok("DecisionGenerated  recommendation=APPROVE")

    # 8. Human review
    await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=app_id, reviewer_id="reviewer-jane",
            final_decision="APPROVE", correlation_id=corr_id,
        ), store)
    ok("HumanReviewCompleted  final_decision=APPROVE")

    elapsed = time.perf_counter() - t0

    # ── Show full event stream ────────────────────────────────────────────────
    events = await store.load_stream(f"loan-{app_id}")
    print(f"\n  {B}Full event stream for loan-{app_id}:{X}")
    for e in events:
        causal = e.metadata.get("correlation_id", "")[:8]
        print(f"    [{e.stream_position:02d}] gpos={e.global_position:04d}  "
              f"{e.event_type:<35} corr={causal}")

    # ── Causal links ─────────────────────────────────────────────────────────
    print(f"\n  {B}Causal links (correlation_id):{X}")
    corr_ids = {e.metadata.get("correlation_id") for e in events if e.metadata.get("correlation_id")}
    for c in corr_ids:
        linked = [e.event_type for e in events if e.metadata.get("correlation_id") == c]
        print(f"    corr={c[:8]}  →  {' → '.join(linked)}")

    # ── Run daemon to catch up all projections (compliance events on separate stream) ──
    compliance_proj = ComplianceAuditViewProjection()
    app_proj = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store, [app_proj, compliance_proj])
    await daemon.poll_until_caught_up()

    # ── Compliance state ──────────────────────────────────────────────────────
    async with pool.acquire() as conn:
        comp_state = await compliance_proj.get_current_compliance(app_id, conn)
    print(f"\n  {B}Compliance state:{X}")
    print(f"    passed_checks={list(comp_state.passed_checks.keys())}  clearance={comp_state.clearance_issued}")

    # ── Cryptographic integrity ───────────────────────────────────────────────
    result = await run_integrity_check(store, "loan", app_id)
    print(f"\n  {B}Cryptographic integrity:{X}")
    print(f"    events_verified={result.events_verified}  chain_valid={result.chain_valid}")
    print(f"    integrity_hash={result.integrity_hash[:24]}...")
    print(f"    tamper_detected={result.tamper_detected}")

    # ── ApplicationSummary projection ─────────────────────────────────────────
    async with pool.acquire() as conn:
        summary = await app_proj.get_current(app_id, conn)
    fraud = round(float(summary['fraud_score']), 2) if summary['fraud_score'] is not None else None
    print(f"\n  {B}ApplicationSummary projection:{X}")
    print(f"    state={summary['state']}  decision={summary['decision']}  "
          f"risk_tier={summary['risk_tier']}  fraud_score={fraud}")

    sla = "✓ PASS" if elapsed < 60 else "✗ FAIL"
    print(f"\n  {B}⏱  Total time: {elapsed:.3f}s  SLA <60s: {sla}{X}")

    return app_id


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Concurrency under pressure (double-decision test)
# ─────────────────────────────────────────────────────────────────────────────

async def step2_concurrency(pool: asyncpg.Pool) -> None:
    hdr(2, "Concurrency Under Pressure (OptimisticConcurrencyError)")

    from src.event_store import EventStore, NewEvent, OptimisticConcurrencyError

    store = EventStore(pool)
    stream_id = f"loan-concurrent-{uuid.uuid4().hex[:8]}"

    # Seed the stream at version 1
    await store.append(
        stream_id,
        [NewEvent("ApplicationSubmitted", {"application_id": stream_id})],
        expected_version=-1,
        aggregate_type="LoanApplication",
    )
    ok(f"Stream seeded at version 1: {stream_id}")

    # Two agents both read version=1 and try to append simultaneously
    async def agent_append(name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await store.append(
                stream_id,
                [NewEvent("CreditAnalysisCompleted", {"agent": name, "application_id": stream_id})],
                expected_version=1,
                aggregate_type="LoanApplication",
            )
            ok(f"Agent {name}: append SUCCEEDED → stream now at version 2")
        except OptimisticConcurrencyError as e:
            warn(f"Agent {name}: OptimisticConcurrencyError  "
                 f"expected={e.expected}  actual={e.actual}")
            # Retry after reload
            current = await store.stream_version(stream_id)
            await store.append(
                stream_id,
                [NewEvent("CreditAnalysisCompleted", {"agent": name + "-retry", "application_id": stream_id})],
                expected_version=current,
                aggregate_type="LoanApplication",
            )
            ok(f"Agent {name}: retry SUCCEEDED after reload → version {current + 1}")

    await asyncio.gather(
        agent_append("Alpha", 0.0),
        agent_append("Beta",  0.01),   # tiny offset so both read v=1 before either commits
    )

    final_version = await store.stream_version(stream_id)
    ok(f"Final stream version: {final_version}  (both events recorded, no data lost)")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Temporal compliance query (as_of)
# ─────────────────────────────────────────────────────────────────────────────

async def step3_temporal_compliance(pool: asyncpg.Pool, app_id: str) -> None:
    hdr(3, "Temporal Compliance Query (as_of timestamp)")

    from src.projections.compliance_audit import ComplianceAuditViewProjection

    proj = ComplianceAuditViewProjection()

    # Capture a past timestamp (before any compliance events for a new app)
    past_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
    now_ts  = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        past_state = await proj.get_compliance_at(app_id, past_ts, conn)
        now_state  = await proj.get_compliance_at(app_id, now_ts,  conn)

    print(f"\n  {B}Compliance at 2020-01-01 (before any events):{X}")
    print(f"    passed_checks={list(past_state.passed_checks.keys())}  "
          f"clearance={past_state.clearance_issued}")

    print(f"\n  {B}Compliance at NOW (after KYC-001 + AML-002 passed):{X}")
    print(f"    passed_checks={list(now_state.passed_checks.keys())}  "
          f"clearance={now_state.clearance_issued}")

    assert past_state.passed_checks == {}, "Past state should have no checks"
    assert "KYC-001" in now_state.passed_checks, "Current state should have KYC-001"
    ok("Temporal query returns distinct states — time-travel verified")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Upcasting & immutability
# ─────────────────────────────────────────────────────────────────────────────

async def step4_upcasting(pool: asyncpg.Pool) -> None:
    hdr(4, "Upcasting & Immutability")

    from src.event_store import EventStore, NewEvent
    import src.upcasting.upcasters  # registers upcasters as side-effect  # noqa: F401

    store = EventStore(pool)
    stream_id = f"loan-upcast-{uuid.uuid4().hex[:8]}"

    # Write a v1 CreditAnalysisCompleted (no model_version / confidence_score fields)
    v1_payload = {
        "application_id": stream_id,
        "agent_id": "legacy-agent",
        "session_id": "s1",
        "risk_tier": "LOW",
        "recommended_limit_usd": 10000,
        "analysis_duration_ms": 200,
        "input_data_hash": "abc123",
    }
    await store.append(
        stream_id,
        [NewEvent("CreditAnalysisCompleted", v1_payload, event_version=1)],
        expected_version=-1,
        aggregate_type="LoanApplication",
    )
    ok(f"Stored v1 CreditAnalysisCompleted (no model_version field)")

    # Read back — upcaster should have run transparently
    events = await store.load_stream(stream_id)
    e = events[0]
    print(f"\n  {B}Event as returned by load_stream (after upcasting):{X}")
    print(f"    event_version : {e.event_version}  (was 1, now 2)")
    print(f"    model_version : {e.payload.get('model_version')!r}")
    print(f"    confidence_score: {e.payload.get('confidence_score')!r}  (None = honest unknown)")
    print(f"    regulatory_basis: {e.payload.get('regulatory_basis')!r}")

    assert e.event_version == 2, "Upcaster must advance version to 2"
    assert e.payload["model_version"] == "legacy-pre-2026"
    ok("Upcasted event arrives as v2 in memory")

    # Verify raw DB row is unchanged
    async with pool.acquire() as conn:
        raw = await conn.fetchrow(
            "SELECT event_version, payload FROM events WHERE stream_id = $1",
            stream_id,
        )
    import json
    raw_payload = json.loads(raw["payload"]) if isinstance(raw["payload"], str) else raw["payload"]
    print(f"\n  {B}Raw DB row (immutable — never touched by upcaster):{X}")
    print(f"    event_version : {raw['event_version']}  (still 1 in DB)")
    print(f"    model_version in payload: {'model_version' in raw_payload}  (absent — v1 schema)")

    assert raw["event_version"] == 1, "DB row must remain at version 1"
    assert "model_version" not in raw_payload, "v1 payload must not have model_version"
    ok("Stored payload is unchanged — immutability preserved")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Gas Town recovery (crash + reconstruct)
# ─────────────────────────────────────────────────────────────────────────────

async def step5_gas_town(pool: asyncpg.Pool) -> None:
    hdr(5, "Gas Town Recovery (crash simulation + reconstruct_agent_context)")

    from src.event_store import EventStore
    from src.commands.handlers import (
        StartAgentSessionCommand, handle_start_agent_session,
        SubmitApplicationCommand, handle_submit_application,
        CreditAnalysisCompletedCommand, handle_credit_analysis_completed,
    )
    from src.aggregates.loan_application import LoanApplicationAggregate
    from src.integrity.gas_town import reconstruct_agent_context

    store = EventStore(pool)
    agent_id = "crash-agent"
    session_id = uuid.uuid4().hex[:8]
    app_id = f"crash-app-{uuid.uuid4().hex[:8]}"

    # Start session and do some work
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="p-99",
                                 requested_amount_usd=5000), store)
    await handle_start_agent_session(
        StartAgentSessionCommand(agent_id=agent_id, session_id=session_id,
                                 model_version="apex-v2.1"), store)

    app = await LoanApplicationAggregate.load(store, app_id)
    await app.request_credit_analysis(store, assigned_agent_id=agent_id)

    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="apex-v2.1", confidence_score=0.75,
            risk_tier="LOW", recommended_limit_usd=5000, duration_ms=150,
        ), store)

    ok(f"Agent appended 3 events to session agent-{agent_id}-{session_id}")
    info(">>> SIMULATING CRASH — process killed here <<<")

    # ── Reconstruct after "crash" ─────────────────────────────────────────────
    ctx = await reconstruct_agent_context(store, agent_id, session_id)

    print(f"\n  {B}Reconstructed context:{X}")
    print(f"    session_health_status : {ctx.session_health_status}")
    print(f"    last_event_position   : {ctx.last_event_position}")
    print(f"    model_version         : {ctx.model_version}")
    print(f"    pending_work          : {ctx.pending_work}")
    print(f"\n  {B}Context text (first 400 chars):{X}")
    print("    " + ctx.context_text[:400].replace("\n", "\n    "))

    assert ctx.last_event_position > 0, "Must have events"
    assert ctx.model_version == "apex-v2.1"
    ok("Agent can resume with correct state — Gas Town recovery verified")

    # ── Prove resumption: agent executes a successful action using reconstructed context ──
    info("Resuming agent using reconstructed context...")
    from src.commands.handlers import FraudScreeningCompletedCommand, handle_fraud_screening_completed
    await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id,
            agent_id=agent_id,
            session_id=session_id,
            fraud_score=0.08,
            anomaly_flags=[],
            screening_model_version="fraud-v1.3",
        ), store)

    # Verify the new event landed on the session stream
    resumed_events = await store.load_stream(f"agent-{agent_id}-{session_id}")
    last = resumed_events[-1]
    ok(f"FraudScreeningCompleted appended at stream_position={last.stream_position}  "
       f"— agent resumed successfully from position {ctx.last_event_position}")
    assert last.event_type == "FraudScreeningCompleted"
    assert last.stream_position == ctx.last_event_position + 1


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — What-If counterfactual (HIGH risk tier instead of MEDIUM)
# ─────────────────────────────────────────────────────────────────────────────

async def step6_whatif(pool: asyncpg.Pool, app_id: str) -> None:
    hdr(6, "What-If Counterfactual (HIGH risk tier → cascading effect)")

    from src.event_store import EventStore, NewEvent
    from src.whatif.projector import run_what_if
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection

    store = EventStore(pool)
    projections = [ApplicationSummaryProjection(), ComplianceAuditViewProjection()]

    # Load the real CreditAnalysisCompleted to get its payload
    events = await store.load_stream(f"loan-{app_id}")
    real_credit = next(e for e in events if e.event_type == "CreditAnalysisCompleted")

    # Counterfactual: same event but risk_tier=HIGH
    cf_payload = {**real_credit.payload, "risk_tier": "HIGH", "recommended_limit_usd": 0}
    counterfactual_event = NewEvent(
        event_type="CreditAnalysisCompleted",
        payload=cf_payload,
        event_version=real_credit.event_version,
    )

    result = await run_what_if(
        store=store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[counterfactual_event],
        projections=projections,
    )

    print(f"\n  {B}Branch point:{X} {result.branch_point}")
    print(f"  {B}Skipped real events (causally dependent):{X} {result.skipped_real_events}")
    print(f"  {B}Counterfactual events injected:{X}  {result.counterfactual_events_injected}")
    print(f"  {B}Divergence events:{X}              {result.divergence_events}")

    real_summary = result.real_outcome.get("ApplicationSummary") or {}
    cf_summary   = result.counterfactual_outcome.get("ApplicationSummary") or {}

    print(f"\n  {B}Real timeline:{X}")
    print(f"    risk_tier={real_summary.get('risk_tier')}  "
          f"decision={real_summary.get('decision')}  state={real_summary.get('state')}")

    print(f"\n  {B}Counterfactual timeline (HIGH risk):{X}")
    print(f"    risk_tier={cf_summary.get('risk_tier')}  "
          f"decision={cf_summary.get('decision')}  state={cf_summary.get('state')}")

    ok("Counterfactual replayed in rolled-back transaction — real store untouched")

    # Verify real store is unchanged
    events_after = await store.load_stream(f"loan-{app_id}")
    real_credit_after = next(e for e in events_after if e.event_type == "CreditAnalysisCompleted")
    assert real_credit_after.payload["risk_tier"] == "MEDIUM", "Real store must be unchanged"
    ok("Real store verified unchanged — immutability preserved")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    print(f"\n{B}{'═'*60}")
    print("  THE LEDGER — Video Demo")
    print(f"{'═'*60}{X}")

    pool = await fresh_pool()
    try:
        # Steps 1–3 share the same application
        app_id = await step1_decision_history(pool)
        await step2_concurrency(pool)
        await step3_temporal_compliance(pool, app_id)
        await step4_upcasting(pool)
        await step5_gas_town(pool)
        await step6_whatif(pool, app_id)

        print(f"\n{B}{G}{'═'*60}")
        print("  ALL 6 STEPS COMPLETE")
        print(f"{'═'*60}{X}\n")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

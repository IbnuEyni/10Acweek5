"""
tests/test_projections.py
=========================
Phase 3 projection tests.

Covers:
- ApplicationSummaryProjection: state tracking, all event types
- AgentPerformanceLedgerProjection: metric accumulation, rates
- ComplianceAuditViewProjection: temporal query, snapshot, rebuild
- ProjectionDaemon: fault tolerance (bad event skipped), per-projection
  checkpoints, get_lag(), get_all_lags()
- SLO: ApplicationSummary lag < 500ms under 50 concurrent command handlers
- SLO: ComplianceAuditView lag < 2000ms under 50 concurrent command handlers
- rebuild_from_scratch: truncate + replay without downtime
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone

import pytest

from src.event_store import EventStore, NewEvent
from src.projections import (
    ApplicationSummaryProjection,
    AgentPerformanceLedgerProjection,
    ComplianceAuditViewProjection,
    ProjectionDaemon,
)
from src.aggregates.loan_application import LoanApplicationAggregate
from src.aggregates.agent_session import AgentSessionAggregate
from src.commands.handlers import (
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    CreditAnalysisCompletedCommand,
    handle_start_agent_session,
    handle_submit_application,
    handle_credit_analysis_completed,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app_summary():
    return ApplicationSummaryProjection()


@pytest.fixture
def agent_perf():
    return AgentPerformanceLedgerProjection()


@pytest.fixture
def compliance_audit():
    return ComplianceAuditViewProjection()


@pytest.fixture
def daemon(store, app_summary, agent_perf, compliance_audit):
    return ProjectionDaemon(
        store,
        [app_summary, agent_perf, compliance_audit],
        max_retries=3,
        batch_size=200,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_daemon_until_caught_up(daemon: ProjectionDaemon, timeout: float = 5.0) -> None:
    """Run daemon batches until all projections are caught up or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        processed = await daemon._process_batch()
        if processed == 0:
            return
    raise TimeoutError("Daemon did not catch up within timeout")


async def _full_lifecycle(store: EventStore, app_id: str, agent_id: str, session_id: str) -> None:
    """Drive a complete loan lifecycle: Submitted → FinalApproved."""
    await handle_start_agent_session(
        StartAgentSessionCommand(agent_id=agent_id, session_id=session_id,
                                 model_version="v2.0"), store
    )
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="test-user",
                                 requested_amount_usd=50_000.0), store
    )
    loan = await LoanApplicationAggregate.load(store, app_id)
    await loan.request_credit_analysis(store, assigned_agent_id=agent_id)

    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="v2.0", confidence_score=0.85, risk_tier="LOW",
            recommended_limit_usd=50_000.0, duration_ms=100,
        ), store
    )

    loan = await LoanApplicationAggregate.load(store, app_id)
    session_stream = f"agent-{agent_id}-{session_id}"
    await loan.request_compliance_review(store, "reg-v1", ["KYC"])
    await loan.record_compliance_passed(store, "KYC", "reg-v1", "h1")
    await loan.generate_decision(
        store, "orch-1", "APPROVE", 0.85, [session_stream], "good", {}
    )
    await loan.complete_human_review(store, "reviewer-1", "APPROVE")
    await loan.approve(store, 50_000.0, 0.05, [], "reviewer-1", "2026-01-01")


# ===========================================================================
# ApplicationSummaryProjection
# ===========================================================================

async def test_app_summary_submitted(store, pool, app_summary):
    app_id = str(uuid.uuid4())
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="alice",
                                 requested_amount_usd=100_000.0), store
    )
    async with pool.acquire() as conn:
        events = await store.load_stream(f"loan-{app_id}")
        for event in events:
            if event.event_type in app_summary.event_types:
                await app_summary.handle(event, conn)
        row = await app_summary.get_current(app_id, conn)

    assert row is not None
    assert row["state"] == "Submitted"
    assert row["applicant_id"] == "alice"
    assert float(row["requested_amount_usd"]) == 100_000.0


async def test_app_summary_full_lifecycle(store, pool, app_summary):
    app_id, agent_id, session_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await _full_lifecycle(store, app_id, agent_id, session_id)

    async with pool.acquire() as conn:
        events_all = await store.load_stream(f"loan-{app_id}")
        for event in events_all:
            if event.event_type in app_summary.event_types:
                await app_summary.handle(event, conn)
        row = await app_summary.get_current(app_id, conn)

    assert row["state"] == "FinalApproved"
    assert float(row["approved_amount_usd"]) == 50_000.0
    assert row["final_decision_at"] is not None
    assert row["human_reviewer_id"] == "reviewer-1"


async def test_app_summary_risk_tier_and_fraud_score(store, pool, app_summary):
    app_id, agent_id, session_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    await handle_start_agent_session(
        StartAgentSessionCommand(agent_id=agent_id, session_id=session_id,
                                 model_version="v2.0"), store
    )
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="bob",
                                 requested_amount_usd=30_000.0), store
    )
    loan = await LoanApplicationAggregate.load(store, app_id)
    await loan.request_credit_analysis(store, assigned_agent_id=agent_id)
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="v2.0", confidence_score=0.7, risk_tier="MEDIUM",
            recommended_limit_usd=30_000.0, duration_ms=80,
        ), store
    )

    # Append a FraudScreeningCompleted event directly
    async with pool.acquire() as conn:
        await store.append(
            stream_id=f"agent-{agent_id}-{session_id}",
            events=[NewEvent("FraudScreeningCompleted", {
                "application_id": app_id, "agent_id": agent_id,
                "fraud_score": 0.03, "anomaly_flags": [],
                "screening_model_version": "fraud-v1", "input_data_hash": "h"
            })],
            expected_version=await store.stream_version(f"agent-{agent_id}-{session_id}"),
        )

    async with pool.acquire() as conn:
        # Process all events through the projection
        async for event in store.load_all(event_types=app_summary.event_types):
            if event.payload.get("application_id") == app_id:
                await app_summary.handle(event, conn)
        row = await app_summary.get_current(app_id, conn)

    assert row["risk_tier"] == "MEDIUM"
    assert float(row["fraud_score"]) == 0.03


# ===========================================================================
# AgentPerformanceLedgerProjection
# ===========================================================================

async def test_agent_perf_credit_analysis_metrics(store, pool, agent_perf):
    agent_id, session_id = str(uuid.uuid4()), str(uuid.uuid4())
    app_id = str(uuid.uuid4())

    await handle_start_agent_session(
        StartAgentSessionCommand(agent_id=agent_id, session_id=session_id,
                                 model_version="v2.0"), store
    )
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="carol",
                                 requested_amount_usd=40_000.0), store
    )
    loan = await LoanApplicationAggregate.load(store, app_id)
    await loan.request_credit_analysis(store, assigned_agent_id=agent_id)
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id, agent_id=agent_id, session_id=session_id,
            model_version="v2.0", confidence_score=0.9, risk_tier="LOW",
            recommended_limit_usd=40_000.0, duration_ms=150,
        ), store
    )

    async with pool.acquire() as conn:
        async for event in store.load_all(event_types=agent_perf.event_types):
            await agent_perf.handle(event, conn)
        metrics = await agent_perf.get_metrics(agent_id, "v2.0", conn)

    assert metrics is not None
    assert metrics["analyses_completed"] == 1
    assert float(metrics["avg_confidence_score"]) == pytest.approx(0.9, abs=0.01)
    assert float(metrics["avg_duration_ms"]) == pytest.approx(150.0, abs=1.0)


async def test_agent_perf_approve_rate(store, pool, agent_perf):
    """approve_rate = approve_count / decisions_generated."""
    orch_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        # Inject two DecisionGenerated events directly
        for rec in [("APPROVE", 0.9), ("DECLINE", 0.8)]:
            await store.append(
                stream_id=f"loan-{uuid.uuid4()}",
                events=[NewEvent("DecisionGenerated", {
                    "application_id": str(uuid.uuid4()),
                    "orchestrator_agent_id": orch_id,
                    "recommendation": rec[0],
                    "confidence_score": rec[1],
                    "contributing_agent_sessions": [],
                    "decision_basis_summary": "",
                    "model_versions": {},
                    "forced_refer": False,
                })],
                expected_version=-1,
                aggregate_type="LoanApplication",
            )

        async for event in store.load_all(event_types=agent_perf.event_types):
            if event.payload.get("orchestrator_agent_id") == orch_id:
                await agent_perf.handle(event, conn)

        metrics = await agent_perf.get_metrics(orch_id, "orchestrator", conn)

    assert metrics is not None
    assert metrics["decisions_generated"] == 2
    assert float(metrics["approve_rate"]) == pytest.approx(0.5, abs=0.01)
    assert float(metrics["decline_rate"]) == pytest.approx(0.5, abs=0.01)


# ===========================================================================
# ComplianceAuditViewProjection
# ===========================================================================

async def test_compliance_audit_current(store, pool, compliance_audit):
    app_id = str(uuid.uuid4())
    compliance_stream = f"compliance-{app_id}"

    await store.append(
        stream_id=compliance_stream,
        events=[
            NewEvent("ComplianceCheckRequested", {
                "application_id": app_id,
                "regulation_set_version": "reg-v2",
                "checks_required": ["KYC", "AML"],
            }),
            NewEvent("ComplianceRulePassed", {
                "application_id": app_id, "rule_id": "KYC",
                "rule_version": "reg-v2", "evidence_hash": "h1",
            }),
            NewEvent("ComplianceRulePassed", {
                "application_id": app_id, "rule_id": "AML",
                "rule_version": "reg-v2", "evidence_hash": "h2",
            }),
        ],
        expected_version=-1,
        aggregate_type="ComplianceRecord",
    )

    async with pool.acquire() as conn:
        async for event in store.load_all(event_types=compliance_audit.event_types):
            await compliance_audit.handle(event, conn)
        state = await compliance_audit.get_current_compliance(app_id, conn)

    assert state.regulation_set_version == "reg-v2"
    assert "KYC" in state.passed_checks
    assert "AML" in state.passed_checks
    assert len(state.failed_checks) == 0


async def test_compliance_audit_temporal_query(store, pool, compliance_audit):
    """get_compliance_at(ts) returns state as it existed at that moment."""
    app_id = str(uuid.uuid4())
    compliance_stream = f"compliance-{app_id}"

    # Append initial check request + KYC pass
    await store.append(
        stream_id=compliance_stream,
        events=[
            NewEvent("ComplianceCheckRequested", {
                "application_id": app_id,
                "regulation_set_version": "reg-v1",
                "checks_required": ["KYC", "AML"],
            }),
            NewEvent("ComplianceRulePassed", {
                "application_id": app_id, "rule_id": "KYC",
                "rule_version": "reg-v1", "evidence_hash": "h1",
            }),
        ],
        expected_version=-1,
        aggregate_type="ComplianceRecord",
    )

    # Capture timestamp between KYC pass and AML pass
    async with pool.acquire() as conn:
        async for event in store.load_all(event_types=compliance_audit.event_types):
            await compliance_audit.handle(event, conn)

    # Record the timestamp after KYC but before AML
    mid_timestamp = datetime.now(timezone.utc)

    # Now append AML pass
    await store.append(
        stream_id=compliance_stream,
        events=[
            NewEvent("ComplianceRulePassed", {
                "application_id": app_id, "rule_id": "AML",
                "rule_version": "reg-v1", "evidence_hash": "h2",
            }),
        ],
        expected_version=2,
        aggregate_type="ComplianceRecord",
    )

    async with pool.acquire() as conn:
        async for event in store.load_all(event_types=compliance_audit.event_types):
            await compliance_audit.handle(event, conn)

        # Query at mid_timestamp — should see KYC but NOT AML
        state_at_mid = await compliance_audit.get_compliance_at(app_id, mid_timestamp, conn)
        # Query current — should see both
        state_current = await compliance_audit.get_current_compliance(app_id, conn)

    assert "KYC" in state_at_mid.passed_checks
    assert "AML" not in state_at_mid.passed_checks

    assert "KYC" in state_current.passed_checks
    assert "AML" in state_current.passed_checks


async def test_compliance_audit_failed_then_passed(store, pool, compliance_audit):
    """A failed check overridden by a pass is reflected correctly."""
    app_id = str(uuid.uuid4())
    compliance_stream = f"compliance-{app_id}"

    await store.append(
        stream_id=compliance_stream,
        events=[
            NewEvent("ComplianceCheckRequested", {
                "application_id": app_id,
                "regulation_set_version": "reg-v1",
                "checks_required": ["KYC"],
            }),
            NewEvent("ComplianceRuleFailed", {
                "application_id": app_id, "rule_id": "KYC",
                "rule_version": "reg-v1", "failure_reason": "missing docs",
            }),
            NewEvent("ComplianceRulePassed", {
                "application_id": app_id, "rule_id": "KYC",
                "rule_version": "reg-v1", "evidence_hash": "h1",
            }),
        ],
        expected_version=-1,
        aggregate_type="ComplianceRecord",
    )

    async with pool.acquire() as conn:
        async for event in store.load_all(event_types=compliance_audit.event_types):
            await compliance_audit.handle(event, conn)
        state = await compliance_audit.get_current_compliance(app_id, conn)

    assert "KYC" in state.passed_checks
    assert "KYC" not in state.failed_checks


async def test_compliance_audit_rebuild_from_scratch(store, pool, compliance_audit):
    """rebuild() truncates tables and resets checkpoint to 0."""
    app_id = str(uuid.uuid4())
    compliance_stream = f"compliance-{app_id}"

    await store.append(
        stream_id=compliance_stream,
        events=[
            NewEvent("ComplianceCheckRequested", {
                "application_id": app_id,
                "regulation_set_version": "reg-v1",
                "checks_required": ["KYC"],
            }),
            NewEvent("ComplianceRulePassed", {
                "application_id": app_id, "rule_id": "KYC",
                "rule_version": "reg-v1", "evidence_hash": "h1",
            }),
        ],
        expected_version=-1,
        aggregate_type="ComplianceRecord",
    )

    async with pool.acquire() as conn:
        async for event in store.load_all(event_types=compliance_audit.event_types):
            await compliance_audit.handle(event, conn)

        # Verify data exists before rebuild
        state_before = await compliance_audit.get_current_compliance(app_id, conn)
        assert "KYC" in state_before.passed_checks

        # Rebuild — truncates tables
        await compliance_audit.rebuild(conn)

        # After rebuild: no data (live reads return empty state)
        state_after = await compliance_audit.get_current_compliance(app_id, conn)
        assert len(state_after.passed_checks) == 0

        # Checkpoint reset to 0
        row = await conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            compliance_audit.name,
        )
        assert row["last_position"] == 0


# ===========================================================================
# ProjectionDaemon — fault tolerance
# ===========================================================================

async def test_daemon_skips_bad_event_after_max_retries(store, pool, daemon):
    """
    A projection that always raises on a specific event must be skipped
    after max_retries — the daemon must not crash and must continue.
    """
    from src.projections.base import Projection
    from src.event_store import RecordedEvent

    class BrokenProjection(Projection):
        name = "BrokenProjection"
        event_types = ["ApplicationSubmitted"]
        call_count = 0

        async def handle(self, event: RecordedEvent, conn) -> None:
            self.call_count += 1
            raise RuntimeError("intentional failure")

        async def rebuild(self, conn) -> None:
            pass

    broken = BrokenProjection()
    bad_daemon = ProjectionDaemon(store, [broken], max_retries=2, batch_size=50)

    app_id = str(uuid.uuid4())
    await handle_submit_application(
        SubmitApplicationCommand(application_id=app_id, applicant_id="fail-test",
                                 requested_amount_usd=1_000.0), store
    )

    # Run enough batches to exhaust retries and skip
    for _ in range(5):
        await bad_daemon._process_batch()

    # Daemon is still running (no crash), broken projection was called max_retries times
    assert broken.call_count == bad_daemon._max_retries


async def test_daemon_per_projection_checkpoints(store, pool, daemon):
    """Each projection advances its checkpoint independently."""
    app_id, agent_id, session_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await _full_lifecycle(store, app_id, agent_id, session_id)

    await _run_daemon_until_caught_up(daemon)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT projection_name, last_position FROM projection_checkpoints"
        )
        checkpoints = {r["projection_name"]: r["last_position"] for r in rows}

    # All three projections must have advanced past 0
    assert checkpoints.get("ApplicationSummary", 0) > 0
    assert checkpoints.get("AgentPerformanceLedger", 0) > 0
    assert checkpoints.get("ComplianceAuditView", 0) > 0


async def test_daemon_get_all_lags_returns_all_projections(store, pool, daemon):
    """get_all_lags() must return an entry for every registered projection."""
    app_id, agent_id, session_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await _full_lifecycle(store, app_id, agent_id, session_id)
    await _run_daemon_until_caught_up(daemon)

    lags = daemon.get_all_lags()
    assert "ApplicationSummary" in lags
    assert "AgentPerformanceLedger" in lags
    assert "ComplianceAuditView" in lags
    for lag in lags.values():
        assert lag >= 0.0


async def test_daemon_rebuild_resets_and_replays(store, pool, daemon):
    """rebuild_projection() resets checkpoint; daemon replays from 0."""
    app_id, agent_id, session_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await _full_lifecycle(store, app_id, agent_id, session_id)
    await _run_daemon_until_caught_up(daemon)

    # Verify data exists
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM application_summary WHERE application_id = $1", app_id
        )
    assert row is not None

    # Rebuild
    await daemon.rebuild_projection("ApplicationSummary")

    # After rebuild: row gone (live reads return nothing)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM application_summary WHERE application_id = $1", app_id
        )
    assert row is None

    # Replay — daemon processes from position 0
    await _run_daemon_until_caught_up(daemon)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state FROM application_summary WHERE application_id = $1", app_id
        )
    assert row is not None
    assert row["state"] == "FinalApproved"


# ===========================================================================
# SLO Tests — lag under 50 concurrent command handlers
# ===========================================================================

async def test_slo_application_summary_lag_under_500ms(store, pool, daemon):
    """
    SLO: ApplicationSummary lag < 500ms under 50 concurrent command handlers.

    Spawns 50 concurrent submit_application tasks, then runs the daemon
    until caught up. Measures wall-clock time from last event write to
    daemon fully caught up.
    """
    N = 50

    async def submit_one():
        app_id = str(uuid.uuid4())
        await handle_submit_application(
            SubmitApplicationCommand(
                application_id=app_id, applicant_id="slo-test",
                requested_amount_usd=10_000.0
            ), store
        )

    # Write 50 events concurrently
    await asyncio.gather(*[submit_one() for _ in range(N)])

    t0 = time.monotonic()
    await _run_daemon_until_caught_up(daemon, timeout=10.0)
    elapsed_ms = (time.monotonic() - t0) * 1000

    # Verify all 50 rows exist in the projection
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM application_summary")
    assert count >= N

    # SLO: daemon catches up within 500ms of the last write
    assert elapsed_ms < 500, (
        f"ApplicationSummary SLO violated: caught up in {elapsed_ms:.1f}ms (limit 500ms)"
    )


async def test_slo_compliance_audit_lag_under_2000ms(store, pool, daemon):
    """
    SLO: ComplianceAuditView lag < 2000ms under 50 concurrent compliance events.
    """
    N = 50

    async def write_compliance_event():
        app_id = str(uuid.uuid4())
        await store.append(
            stream_id=f"compliance-{app_id}",
            events=[
                NewEvent("ComplianceCheckRequested", {
                    "application_id": app_id,
                    "regulation_set_version": "reg-v1",
                    "checks_required": ["KYC"],
                }),
                NewEvent("ComplianceRulePassed", {
                    "application_id": app_id, "rule_id": "KYC",
                    "rule_version": "reg-v1", "evidence_hash": "h",
                }),
            ],
            expected_version=-1,
            aggregate_type="ComplianceRecord",
        )

    await asyncio.gather(*[write_compliance_event() for _ in range(N)])

    t0 = time.monotonic()
    await _run_daemon_until_caught_up(daemon, timeout=15.0)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert elapsed_ms < 2000, (
        f"ComplianceAuditView SLO violated: caught up in {elapsed_ms:.1f}ms (limit 2000ms)"
    )

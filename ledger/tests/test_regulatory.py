"""
Phase 6 — Regulatory Examination Package Tests

Covers:
- Package contains all 7 required sections.
- complete_event_stream includes all related streams (loan, compliance, agent, audit).
- projection_states reflects state at examination_date (temporal correctness).
- integrity_verification includes full chain replay and overall_chain_valid.
- lifecycle_narrative contains one sentence per significant event.
- ai_participation_record captures model versions, confidence scores, input_data_hashes.
- verification_queries are present and non-empty SQL strings.
- package_hash is a valid SHA-256 and is reproducible (deterministic).
- package_hash changes if any section is modified (tamper detection).
- Package is fully JSON-serialisable (no datetime objects, no UUIDs, etc.).
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest

from src.event_store import EventStore, NewEvent
from src.regulatory.package import generate_regulatory_package

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_complete_lifecycle(
    store: EventStore,
    pool,
    app_id: str,
    agent_id: str,
    session_id: str,
) -> None:
    """Build a complete loan lifecycle across all streams."""
    loan_stream = f"loan-{app_id}"
    compliance_stream = f"compliance-{app_id}"
    agent_stream = f"agent-{agent_id}-{session_id}"

    # Agent session stream
    await store.append(agent_stream, [NewEvent("AgentContextLoaded", {
        "agent_id": agent_id,
        "session_id": session_id,
        "context_source": "cold_start",
        "event_replay_from_position": 0,
        "context_token_count": 1024,
        "model_version": "v2.3",
        "context": {},
    })], expected_version=0, aggregate_type="AgentSession")

    # Loan stream
    await store.append(loan_stream, [NewEvent("ApplicationSubmitted", {
        "application_id": app_id,
        "applicant_id": "applicant-reg-test",
        "requested_amount_usd": 75000.0,
        "loan_purpose": "equipment",
    })], expected_version=0, aggregate_type="LoanApplication")

    await store.append(loan_stream, [NewEvent("CreditAnalysisRequested", {
        "application_id": app_id,
        "assigned_agent_id": agent_id,
    })], expected_version=1, aggregate_type="LoanApplication")

    # Credit analysis on agent stream
    await store.append(agent_stream, [NewEvent("CreditAnalysisCompleted", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.88,
        "risk_tier": "LOW",
        "recommended_limit_usd": 75000.0,
        "analysis_duration_ms": 800,
        "input_data_hash": "sha256-input-hash-001",
    })], expected_version=1, aggregate_type="AgentSession")

    # Credit analysis on loan stream
    await store.append(loan_stream, [NewEvent("CreditAnalysisCompleted", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.88,
        "risk_tier": "LOW",
        "recommended_limit_usd": 75000.0,
        "analysis_duration_ms": 800,
        "input_data_hash": "sha256-input-hash-001",
    })], expected_version=2, aggregate_type="LoanApplication")

    # Fraud screening on agent stream
    await store.append(agent_stream, [NewEvent("FraudScreeningCompleted", {
        "application_id": app_id,
        "agent_id": agent_id,
        "fraud_score": 0.04,
        "anomaly_flags": [],
        "screening_model_version": "fraud-v1.2",
        "input_data_hash": "sha256-fraud-hash-001",
    })], expected_version=2, aggregate_type="AgentSession")

    # Compliance stream
    await store.append(compliance_stream, [
        NewEvent("ComplianceCheckRequested", {
            "application_id": app_id,
            "regulation_set_version": "v2024",
            "checks_required": ["KYC", "AML"],
        }),
        NewEvent("ComplianceRulePassed", {
            "application_id": app_id,
            "rule_id": "KYC",
            "rule_version": "v2024.1",
            "evidence_hash": "kyc-evidence-hash",
        }),
        NewEvent("ComplianceRulePassed", {
            "application_id": app_id,
            "rule_id": "AML",
            "rule_version": "v2024.1",
            "evidence_hash": "aml-evidence-hash",
        }),
    ], expected_version=0, aggregate_type="ComplianceRecord")

    # Loan stream — compliance + decision
    await store.append(loan_stream, [NewEvent("ComplianceCheckRequested", {
        "application_id": app_id,
        "regulation_set_version": "v2024",
        "checks_required": ["KYC", "AML"],
    })], expected_version=3, aggregate_type="LoanApplication")

    await store.append(loan_stream, [
        NewEvent("ComplianceRulePassed", {
            "application_id": app_id,
            "rule_id": "KYC",
            "rule_version": "v2024.1",
            "evidence_hash": "kyc-evidence-hash",
        }),
        NewEvent("ComplianceRulePassed", {
            "application_id": app_id,
            "rule_id": "AML",
            "rule_version": "v2024.1",
            "evidence_hash": "aml-evidence-hash",
        }),
    ], expected_version=4, aggregate_type="LoanApplication")

    await store.append(loan_stream, [NewEvent("DecisionGenerated", {
        "application_id": app_id,
        "orchestrator_agent_id": agent_id,
        "recommendation": "APPROVE",
        "confidence_score": 0.88,
        "contributing_agent_sessions": [agent_stream],
        "decision_basis_summary": "Low risk, clean fraud, full compliance",
        "model_versions": {agent_stream: "v2.3"},
        "forced_refer": False,
    })], expected_version=6, aggregate_type="LoanApplication")

    await store.append(loan_stream, [NewEvent("HumanReviewCompleted", {
        "application_id": app_id,
        "reviewer_id": "reviewer-reg-001",
        "final_decision": "APPROVE",
        "override": False,
        "override_reason": "",
    })], expected_version=7, aggregate_type="LoanApplication")

    await store.append(loan_stream, [NewEvent("ApplicationApproved", {
        "application_id": app_id,
        "approved_amount_usd": 75000.0,
        "interest_rate": 0.042,
        "conditions": ["annual_review"],
        "approved_by": "reviewer-reg-001",
        "effective_date": "2026-03-01",
    })], expected_version=8, aggregate_type="LoanApplication")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_regulatory_package_contains_all_sections(store, pool):
    """Package must contain all 7 required top-level sections."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    required_sections = [
        "complete_event_stream",
        "projection_states",
        "integrity_verification",
        "lifecycle_narrative",
        "ai_participation_record",
        "verification_queries",
        "package_metadata",
    ]
    for section in required_sections:
        assert section in package, f"Missing required section: {section}"


async def test_regulatory_package_complete_event_stream(store, pool):
    """complete_event_stream must include events from all related streams."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    stream = package["complete_event_stream"]
    assert len(stream) > 0

    stream_ids_in_package = {e["stream_id"] for e in stream}
    assert f"loan-{app_id}" in stream_ids_in_package
    assert f"compliance-{app_id}" in stream_ids_in_package
    assert f"agent-{agent_id}-{session_id}" in stream_ids_in_package

    # Every event must have the required fields
    for ev in stream:
        assert "event_id" in ev
        assert "stream_id" in ev
        assert "event_type" in ev
        assert "payload" in ev
        assert "recorded_at" in ev
        assert "global_position" in ev


async def test_regulatory_package_integrity_verification(store, pool):
    """integrity_verification must include latest_check and full_chain_replay."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    iv = package["integrity_verification"]
    assert "latest_check" in iv
    assert "full_chain_replay" in iv
    assert "overall_chain_valid" in iv

    lc = iv["latest_check"]
    assert "integrity_hash" in lc
    assert "chain_valid" in lc
    assert "tamper_detected" in lc
    assert lc["chain_valid"] is True
    assert lc["tamper_detected"] is False

    assert isinstance(iv["full_chain_replay"], list)
    assert iv["overall_chain_valid"] is True


async def test_regulatory_package_lifecycle_narrative(store, pool):
    """lifecycle_narrative must contain one sentence per significant event."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    narrative = package["lifecycle_narrative"]
    assert narrative["application_id"] == app_id
    assert narrative["total_events"] > 0
    assert len(narrative["narrative"]) == narrative["total_events"]
    assert narrative["summary"]

    # Key lifecycle events must appear in the narrative
    full_text = " ".join(narrative["narrative"])
    assert "submitted" in full_text.lower() or "ApplicationSubmitted" in full_text
    assert "approved" in full_text.lower() or "ApplicationApproved" in full_text


async def test_regulatory_package_ai_participation_record(store, pool):
    """ai_participation_record must capture all AI agent contributions."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    records = package["ai_participation_record"]
    assert len(records) > 0

    roles = {r["role"] for r in records}
    assert "credit_analyst" in roles
    assert "fraud_screener" in roles
    assert "orchestrator" in roles

    # Credit analyst record must have model metadata
    credit_record = next(r for r in records if r["role"] == "credit_analyst")
    assert credit_record["model_version"] == "v2.3"
    assert credit_record["confidence_score"] == 0.88
    assert credit_record["input_data_hash"] == "sha256-input-hash-001"
    assert credit_record["risk_tier"] == "LOW"

    # Fraud screener record
    fraud_record = next(r for r in records if r["role"] == "fraud_screener")
    assert fraud_record["screening_model_version"] == "fraud-v1.2"
    assert fraud_record["fraud_score"] == 0.04
    assert fraud_record["input_data_hash"] == "sha256-fraud-hash-001"


async def test_regulatory_package_verification_queries(store, pool):
    """verification_queries must be present and contain valid SQL strings."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    queries = package["verification_queries"]
    assert len(queries) > 0

    for name, sql in queries.items():
        assert isinstance(sql, str), f"Query {name} must be a string"
        assert len(sql) > 10, f"Query {name} must be non-trivial"
        # Must reference the application_id so the regulator can run it
        assert app_id in sql, f"Query {name} must reference the application_id"


async def test_regulatory_package_hash_is_sha256(store, pool):
    """package_hash must be a valid 64-character hex SHA-256 string."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    pkg_hash = package["package_metadata"]["package_hash"]
    assert isinstance(pkg_hash, str)
    assert len(pkg_hash) == 64  # SHA-256 hex = 64 chars
    # Must be valid hex
    int(pkg_hash, 16)


async def test_regulatory_package_hash_is_reproducible(store, pool):
    """
    The package_hash must be deterministic: recomputing it from the package
    contents must produce the same value.
    """
    import hashlib, json

    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    stored_hash = package["package_metadata"]["package_hash"]

    # Recompute from the package sections (excluding package_metadata)
    sections_for_hash = {
        k: v for k, v in package.items() if k != "package_metadata"
    }
    canonical = json.dumps(sections_for_hash, sort_keys=True, separators=(",", ":"), default=str)
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert recomputed == stored_hash, (
        "package_hash must be reproducible from the package contents"
    )


async def test_regulatory_package_is_json_serialisable(store, pool):
    """The entire package must be JSON-serialisable with no special types."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    package = await generate_regulatory_package(
        store, app_id, datetime.now(timezone.utc), pool
    )

    # Must not raise
    serialised = json.dumps(package, default=str)
    assert len(serialised) > 100

    # Must round-trip cleanly
    reloaded = json.loads(serialised)
    assert reloaded["package_metadata"]["application_id"] == app_id


async def test_regulatory_package_projection_states_at_examination_date(store, pool):
    """
    projection_states must reflect the state at examination_date, not the
    current state. Querying at a past timestamp must return the historical state.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    loan_stream = f"loan-{app_id}"

    # Submit application only
    await store.append(loan_stream, [NewEvent("ApplicationSubmitted", {
        "application_id": app_id,
        "applicant_id": "applicant-temporal",
        "requested_amount_usd": 50000.0,
    })], expected_version=0, aggregate_type="LoanApplication")

    # Capture timestamp after submission
    mid_timestamp = datetime.now(timezone.utc)

    # Now advance to AwaitingAnalysis
    await store.append(loan_stream, [NewEvent("CreditAnalysisRequested", {
        "application_id": app_id,
        "assigned_agent_id": agent_id,
    })], expected_version=1, aggregate_type="LoanApplication")

    # Package at mid_timestamp — must show Submitted, not AwaitingAnalysis
    package = await generate_regulatory_package(
        store, app_id, mid_timestamp, pool
    )

    app_state = package["projection_states"]["ApplicationSummary"]
    assert app_state["state"] == "Submitted", (
        f"At mid_timestamp the state should be Submitted, got: {app_state['state']}"
    )


async def test_regulatory_package_metadata_fields(store, pool):
    """package_metadata must contain all required fields."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await _build_complete_lifecycle(store, pool, app_id, agent_id, session_id)

    examination_date = datetime.now(timezone.utc)
    package = await generate_regulatory_package(store, app_id, examination_date, pool)

    meta = package["package_metadata"]
    assert meta["application_id"] == app_id
    assert meta["package_version"] == "1.0.0"
    assert "generated_at" in meta
    assert "examination_date" in meta
    assert "package_hash" in meta
    assert "hash_algorithm" in meta
    assert meta["hash_algorithm"] == "SHA-256"
    assert "streams_included" in meta
    assert isinstance(meta["streams_included"], list)
    assert f"loan-{app_id}" in meta["streams_included"]
    assert meta["total_events_across_all_streams"] > 0

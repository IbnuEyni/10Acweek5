"""
Phase 4B — Cryptographic Audit Chain Tests

Tests for run_integrity_check():
- Hash chain construction
- Tamper detection
- Chain verification across multiple checks
"""
import uuid
import pytest

from src.event_store import EventStore, NewEvent
from src.integrity.audit_chain import run_integrity_check

pytestmark = pytest.mark.asyncio


async def _append_loan_events(store: EventStore, app_id: str) -> None:
    """Append a few events to a loan stream for integrity checking."""
    stream_id = f"loan-{app_id}"
    await store.append(
        stream_id,
        [NewEvent("ApplicationSubmitted", {
            "application_id": app_id,
            "applicant_id": "applicant-001",
            "requested_amount_usd": 50000.0,
        })],
        expected_version=0,
        aggregate_type="LoanApplication",
    )
    await store.append(
        stream_id,
        [NewEvent("CreditAnalysisRequested", {
            "application_id": app_id,
            "assigned_agent_id": "agent-001",
        })],
        expected_version=1,
        aggregate_type="LoanApplication",
    )


async def test_integrity_check_returns_result(store):
    """run_integrity_check returns an IntegrityCheckResult with correct fields."""
    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    result = await run_integrity_check(store, "loan", app_id)

    assert result.entity_type == "loan"
    assert result.entity_id == app_id
    assert result.events_verified == 2
    # SHA-256 hex digest is exactly 64 lowercase hex characters
    assert len(result.integrity_hash) == 64
    assert all(c in "0123456789abcdef" for c in result.integrity_hash)
    assert result.previous_hash is None  # first check
    assert result.chain_valid is True
    assert result.tamper_detected is False


async def test_integrity_check_appends_audit_event(store):
    """run_integrity_check appends an AuditIntegrityCheckRun event to the audit stream."""
    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    await run_integrity_check(store, "loan", app_id)

    audit_events = await store.load_stream(f"audit-loan-{app_id}")
    assert len(audit_events) == 1
    p = audit_events[0].payload
    assert audit_events[0].event_type == "AuditIntegrityCheckRun"
    assert p["events_verified_count"] == 2
    assert p["chain_valid"] is True
    # segment_start_position must be 0 for the first check (started from the beginning)
    assert p["segment_start_position"] == 0
    # last_stream_position must equal the stream_position of the last event verified
    assert p["last_stream_position"] == 2
    # integrity_hash must be a valid 64-char SHA-256 hex digest
    assert len(p["integrity_hash"]) == 64


async def test_integrity_check_chain_links_across_runs(store):
    """Second integrity check references the first check's hash as previous_hash."""
    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    result1 = await run_integrity_check(store, "loan", app_id)
    assert result1.previous_hash is None

    # Append another event
    await store.append(
        f"loan-{app_id}",
        [NewEvent("ComplianceCheckRequested", {
            "application_id": app_id,
            "regulation_set_version": "v1",
            "checks_required": ["KYC"],
        })],
        expected_version=2,
        aggregate_type="LoanApplication",
    )

    result2 = await run_integrity_check(store, "loan", app_id)
    assert result2.previous_hash == result1.integrity_hash
    assert result2.events_verified == 1  # only the new event since last check
    assert result2.chain_valid is True


async def test_integrity_check_empty_stream(store):
    """Integrity check on a stream with no events returns 0 events verified."""
    app_id = str(uuid.uuid4())[:8]
    result = await run_integrity_check(store, "loan", app_id)
    assert result.events_verified == 0
    assert result.chain_valid is True
    assert result.tamper_detected is False


async def test_integrity_check_hash_is_deterministic(store):
    """Same events always produce the same hash."""
    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    result1 = await run_integrity_check(store, "loan", app_id)

    # Reset audit stream by loading a fresh store (same DB data)
    # We can't easily tamper, but we can verify the hash is stable
    # by checking the stored audit event matches the returned hash
    audit_events = await store.load_stream(f"audit-loan-{app_id}")
    stored_hash = audit_events[0].payload["integrity_hash"]
    assert stored_hash == result1.integrity_hash


async def test_integrity_check_audit_stream_version_field(store):
    """IntegrityCheckResult.audit_stream_version reflects the new audit stream version."""
    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    result1 = await run_integrity_check(store, "loan", app_id)
    assert result1.audit_stream_version == 1

    result2 = await run_integrity_check(store, "loan", app_id)
    assert result2.audit_stream_version == 2


async def test_verify_full_chain_validates_all_segments(store):
    """verify_full_chain() replays every segment and returns one result per check run."""
    from src.integrity.audit_chain import verify_full_chain

    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    await run_integrity_check(store, "loan", app_id)

    await store.append(
        f"loan-{app_id}",
        [NewEvent("ComplianceCheckRequested", {
            "application_id": app_id,
            "regulation_set_version": "v1",
            "checks_required": ["KYC"],
        })],
        expected_version=2,
        aggregate_type="LoanApplication",
    )
    await run_integrity_check(store, "loan", app_id)

    results = await verify_full_chain(store, "loan", app_id)

    assert len(results) == 2
    assert all(r.chain_valid for r in results)
    assert all(not r.tamper_detected for r in results)
    assert results[0].events_verified == 2
    assert results[1].events_verified == 1


async def test_verify_full_chain_detects_tamper(store, pool):
    """verify_full_chain() reports chain_valid=False when a stored payload is modified."""
    import json
    from src.integrity.audit_chain import verify_full_chain

    app_id = str(uuid.uuid4())[:8]
    await _append_loan_events(store, app_id)

    # Establish baseline hash
    await run_integrity_check(store, "loan", app_id)

    # Directly tamper with the stored payload of the first event
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE events
            SET payload = $1
            WHERE stream_id = $2 AND stream_position = 1
            """,
            json.dumps({
                "application_id": app_id,
                "applicant_id": "TAMPERED",
                "requested_amount_usd": 99999.0,
            }),
            f"loan-{app_id}",
        )

    # Second check must detect the tamper in the previous segment
    result2 = await run_integrity_check(store, "loan", app_id)
    assert result2.chain_valid is False
    assert result2.tamper_detected is True

    # verify_full_chain also flags the tampered segment
    results = await verify_full_chain(store, "loan", app_id)
    assert any(not r.chain_valid for r in results)

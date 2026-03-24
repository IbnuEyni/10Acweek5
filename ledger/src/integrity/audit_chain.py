"""
Cryptographic audit chain for AuditLedger streams.

Each AuditIntegrityCheckRun event records:
  - integrity_hash: SHA-256(previous_hash + sorted event payloads since last check)
  - previous_hash: the integrity_hash from the prior check (None for first check)
  - events_verified_count: number of events hashed in this check
  - chain_valid: True if the chain is unbroken

Any post-hoc modification of stored events breaks the chain — tamper detection.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.event_store import EventStore, NewEvent


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    integrity_hash: str
    previous_hash: str | None
    chain_valid: bool
    tamper_detected: bool


async def run_integrity_check(
    store: EventStore,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    1. Load all events for the entity's primary stream (e.g. loan-{id}).
    2. Load the last AuditIntegrityCheckRun event from the audit stream (if any).
    3. Hash the payloads of all events since the last check.
    4. Verify hash chain: new_hash = sha256(previous_hash + event_hashes).
    5. Append new AuditIntegrityCheckRun event to audit-{entity_type}-{entity_id}.
    6. Return result with events_verified, chain_valid, tamper_detected.
    """
    # Determine the primary stream for this entity
    primary_stream = f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"

    # Load the audit stream to find the last integrity check
    audit_events = await store.load_stream(audit_stream)
    last_check = None
    for ev in reversed(audit_events):
        if ev.event_type == "AuditIntegrityCheckRun":
            last_check = ev
            break

    previous_hash: str | None = None
    from_position = 0
    if last_check:
        previous_hash = last_check.payload.get("integrity_hash")
        # Resume from the position after the last verified event
        from_position = last_check.payload.get("events_verified_count", 0)

    # Load primary stream events since last check
    primary_events = await store.load_stream(primary_stream, from_position=from_position)

    # Build the hash: sha256(previous_hash_bytes + each_payload_json sorted)
    hasher = hashlib.sha256()
    if previous_hash:
        hasher.update(previous_hash.encode())

    for ev in primary_events:
        # Deterministic serialisation: sort keys, no whitespace
        payload_bytes = json.dumps(ev.payload, sort_keys=True, separators=(",", ":")).encode()
        hasher.update(payload_bytes)

    new_hash = hasher.hexdigest()
    events_verified = len(primary_events)

    # Verify chain integrity: recompute from scratch and compare
    chain_valid = True
    tamper_detected = False
    if last_check:
        # Re-verify the previous segment to detect tampering
        verify_hasher = hashlib.sha256()
        prev_prev_hash = last_check.payload.get("previous_hash")
        if prev_prev_hash:
            verify_hasher.update(prev_prev_hash.encode())
        prev_events = await store.load_stream(primary_stream, to_position=from_position)
        for ev in prev_events:
            payload_bytes = json.dumps(ev.payload, sort_keys=True, separators=(",", ":")).encode()
            verify_hasher.update(payload_bytes)
        recomputed = verify_hasher.hexdigest()
        if recomputed != last_check.payload.get("integrity_hash"):
            chain_valid = False
            tamper_detected = True

    # Append the integrity check event to the audit stream
    audit_version = await store.stream_version(audit_stream)
    await store.append(
        audit_stream,
        [NewEvent(
            event_type="AuditIntegrityCheckRun",
            payload={
                "entity_id": entity_id,
                "events_verified_count": events_verified,
                "integrity_hash": new_hash,
                "previous_hash": previous_hash,
                "chain_valid": chain_valid,
            },
            event_version=1,
        )],
        expected_version=audit_version,
        aggregate_type="AuditLedger",
    )

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        events_verified=events_verified,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
    )

"""
Cryptographic audit chain for AuditLedger streams.

Implements a blockchain-style hash chain over the event log for any entity stream.
Each AuditIntegrityCheckRun event records:

  integrity_hash:       SHA-256(previous_hash || event_id_1 || payload_1 || ... || payload_N)
  previous_hash:        integrity_hash from the prior check (None for the first check)
  events_verified_count: number of events hashed in this check run
  chain_valid:          True if the prior segment's stored hash matches recomputation
  tamper_detected:      True if chain_valid is False

Any post-hoc modification of stored events breaks the chain — tamper is detectable
on the next integrity check run.

Hash construction:
  - Each event contributes: sha256(event_id_bytes || payload_json_sorted)
  - The segment hash is: sha256(previous_hash_bytes || event_hash_1 || ... || event_hash_N)
  - Using event_id in the per-event hash prevents payload-swap attacks where an
    attacker swaps two events' payloads (same content, different position).
  - Deterministic JSON serialisation (sort_keys=True, no whitespace) ensures the
    hash is reproducible across Python versions and platforms.

Chain verification:
  - On each run, the PREVIOUS segment is re-hashed from the raw DB events and
    compared against the stored integrity_hash. This detects tampering in the
    already-verified segment.
  - Only the previous segment is re-verified (not the full history) — O(segment_size),
    not O(total_events). Full chain verification requires replaying all segments
    in order, which is a separate audit operation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from src.event_store import EventStore, NewEvent, RecordedEvent


@dataclass(frozen=True)
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    integrity_hash: str
    previous_hash: str | None
    chain_valid: bool
    tamper_detected: bool
    audit_stream_version: int


def _hash_event(event: RecordedEvent) -> bytes:
    """
    Compute a deterministic hash for a single event.

    Includes event_id to prevent payload-swap attacks:
    two events with identical payloads but different positions
    produce different hashes.
    """
    hasher = hashlib.sha256()
    hasher.update(event.event_id.bytes)
    payload_bytes = json.dumps(
        event.payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    hasher.update(payload_bytes)
    return hasher.digest()


def _hash_segment(previous_hash: str | None, events: list[RecordedEvent]) -> str:
    """
    Compute the integrity hash for a segment of events.

    new_hash = SHA-256(previous_hash_bytes || hash(event_1) || ... || hash(event_N))

    An empty segment (no new events) still produces a valid hash chained to
    the previous hash — the chain is unbroken even during quiet periods.
    """
    hasher = hashlib.sha256()
    if previous_hash:
        hasher.update(previous_hash.encode("utf-8"))
    for event in events:
        hasher.update(_hash_event(event))
    return hasher.hexdigest()


async def run_integrity_check(
    store: EventStore,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    Run a cryptographic integrity check on an entity's event stream.

    Steps:
    1. Load all events for the entity's primary stream (e.g. loan-{id}).
    2. Load the audit stream to find the last AuditIntegrityCheckRun event.
    3. Determine the stream position of the last verified event (not a count).
    4. Hash all events since the last check using _hash_segment().
    5. Verify the previous segment by recomputing its hash from raw DB events.
    6. Append a new AuditIntegrityCheckRun event to audit-{entity_type}-{entity_id}.
    7. Return IntegrityCheckResult with all fields populated.

    Chain verification is O(previous_segment_size), not O(total_events).
    """
    primary_stream = f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"

    # ── Step 1: Load audit stream to find the last integrity check ──────────
    audit_events = await store.load_stream(audit_stream)
    last_check: RecordedEvent | None = None
    for ev in reversed(audit_events):
        if ev.event_type == "AuditIntegrityCheckRun":
            last_check = ev
            break

    # ── Step 2: Determine resume position ───────────────────────────────────
    # last_verified_position is the stream_position of the last event that was
    # included in the previous integrity check. We load events AFTER this position.
    # This is a stream_position (1-based), not a count.
    previous_hash: str | None = None
    last_verified_position: int = 0  # 0 means "load from the beginning"

    if last_check:
        previous_hash = last_check.payload.get("integrity_hash")
        # last_stream_position is stored in the payload — the stream_position
        # of the last event included in the previous check.
        last_verified_position = last_check.payload.get("last_stream_position", 0)

    # ── Step 3: Load new events since last check ─────────────────────────────
    # load_stream uses from_position as exclusive lower bound on stream_position.
    new_events = await store.load_stream(
        primary_stream,
        from_position=last_verified_position,
    )

    # ── Step 4: Hash the new segment ─────────────────────────────────────────
    new_hash = _hash_segment(previous_hash, new_events)
    events_verified = len(new_events)

    # The stream_position of the last event in this segment (for the next check)
    last_stream_position = (
        new_events[-1].stream_position if new_events else last_verified_position
    )

    # ── Step 5: Verify the previous segment (tamper detection) ───────────────
    chain_valid = True
    tamper_detected = False

    if last_check:
        # Re-hash the previous segment from raw DB events and compare.
        # This detects any modification to events in the already-verified range.
        prev_prev_hash: str | None = last_check.payload.get("previous_hash")
        prev_last_position: int = last_check.payload.get("last_stream_position", 0)

        # Load the events that were in the previous segment.
        # The previous segment ran from (prev_prev_last_position, prev_last_position].
        # We need to know where the segment before that ended.
        # Simplification: load all events up to and including prev_last_position,
        # then slice to the segment. For the first check, prev_prev_last_position = 0.
        prev_segment_start = last_check.payload.get("segment_start_position", 0)
        prev_events = await store.load_stream(
            primary_stream,
            from_position=prev_segment_start,
            to_position=prev_last_position,
        )
        recomputed = _hash_segment(prev_prev_hash, prev_events)
        stored = last_check.payload.get("integrity_hash", "")
        if recomputed != stored:
            chain_valid = False
            tamper_detected = True

    # ── Step 6: Append AuditIntegrityCheckRun to the audit stream ────────────
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
                # Store the stream_position boundaries so the next check can
                # re-verify this segment without ambiguity.
                "segment_start_position": last_verified_position,
                "last_stream_position": last_stream_position,
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
        audit_stream_version=audit_version + 1,
    )


async def verify_full_chain(
    store: EventStore,
    entity_type: str,
    entity_id: str,
) -> list[IntegrityCheckResult]:
    """
    Verify the complete hash chain from the beginning.

    Replays all AuditIntegrityCheckRun events in order and recomputes each
    segment's hash. Returns one result per check run.

    This is O(total_events) and intended for regulatory examination, not
    routine monitoring. Use run_integrity_check() for incremental checks.
    """
    primary_stream = f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"

    audit_events = await store.load_stream(audit_stream)
    check_events = [e for e in audit_events if e.event_type == "AuditIntegrityCheckRun"]

    results: list[IntegrityCheckResult] = []
    for check in check_events:
        p = check.payload
        start = p.get("segment_start_position", 0)
        end = p.get("last_stream_position", 0)
        prev_hash = p.get("previous_hash")

        segment = await store.load_stream(primary_stream, from_position=start, to_position=end)
        recomputed = _hash_segment(prev_hash, segment)
        stored = p.get("integrity_hash", "")
        valid = recomputed == stored

        results.append(IntegrityCheckResult(
            entity_type=entity_type,
            entity_id=entity_id,
            events_verified=p.get("events_verified_count", 0),
            integrity_hash=stored,
            previous_hash=prev_hash,
            chain_valid=valid,
            tamper_detected=not valid,
            audit_stream_version=check.stream_position,
        ))

    return results

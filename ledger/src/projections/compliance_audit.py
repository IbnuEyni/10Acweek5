from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from src.event_store import RecordedEvent
from src.projections.base import Projection

# Snapshot trigger: take a snapshot every N compliance events per application.
# Justification in DESIGN.md: event-count trigger balances snapshot overhead
# against replay cost. At 10 events, worst-case replay is 9 events from snapshot.
_SNAPSHOT_EVERY_N = 10


@dataclass
class ComplianceState:
    """
    Full compliance state for one application at a point in time.
    Serialised to JSONB for snapshot storage.
    """
    application_id: str
    regulation_set_version: str | None = None
    checks_required: list[str] = field(default_factory=list)
    passed_checks: dict[str, str] = field(default_factory=dict)   # rule_id → rule_version
    failed_checks: dict[str, str] = field(default_factory=dict)   # rule_id → failure_reason
    clearance_issued: bool = False
    event_count: int = 0  # compliance events processed — drives snapshot trigger

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str | dict) -> "ComplianceState":
        d = json.loads(data) if isinstance(data, str) else data
        return cls(**d)


class ComplianceAuditViewProjection(Projection):
    """
    Regulatory read model — the view a compliance officer queries.

    Requirements:
    - Complete: every compliance event is recorded.
    - Traceable: every rule references its regulation version.
    - Temporally queryable: get_compliance_at(application_id, timestamp).

    Snapshot strategy (event-count trigger):
    - After every _SNAPSHOT_EVERY_N compliance events per application,
      a full ComplianceState snapshot is written to compliance_audit_snapshots.
    - get_compliance_at(ts) finds the latest snapshot before ts, then replays
      only the compliance_audit_events after that snapshot up to ts.
    - Snapshot invalidation: snapshots are never mutated. If a rebuild occurs,
      all snapshots are truncated and rebuilt from the event log.

    SLO: lag < 2000ms.
    """

    name = "ComplianceAuditView"

    event_types = [
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceClearanceIssued",
    ]

    def __init__(self) -> None:
        # Instance-level counter — not shared across daemon instances or test runs.
        # Class-level dicts are a subtle production bug: two daemons or two test
        # fixtures would share state, causing spurious snapshot triggers.
        self._event_counts: dict[str, int] = {}

    async def handle(self, event: RecordedEvent, conn: asyncpg.Connection) -> None:
        p = event.payload
        app_id = p.get("application_id")
        if not app_id:
            return

        # 1. Append to the audit event log (append-only, never updated)
        result_map = {
            "ComplianceCheckRequested": "requested",
            "ComplianceRulePassed":     "passed",
            "ComplianceRuleFailed":     "failed",
            "ComplianceClearanceIssued": "cleared",
        }
        await conn.execute(
            """
            INSERT INTO compliance_audit_events
                (application_id, global_position, event_type, rule_id, rule_version,
                 regulation_set_version, result, failure_reason, evidence_hash, recorded_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT DO NOTHING
            """,
            app_id,
            event.global_position,
            event.event_type,
            p.get("rule_id"),
            p.get("rule_version"),
            p.get("regulation_set_version"),
            result_map.get(event.event_type, "unknown"),
            p.get("failure_reason"),
            p.get("evidence_hash"),
            event.recorded_at,
        )

        # 2. Update in-memory event count and maybe take a snapshot
        self._event_counts[app_id] = self._event_counts.get(app_id, 0) + 1
        if self._event_counts[app_id] % _SNAPSHOT_EVERY_N == 0:
            await self._take_snapshot(app_id, event, conn)

    async def _take_snapshot(
        self,
        app_id: str,
        trigger_event: RecordedEvent,
        conn: asyncpg.Connection,
    ) -> None:
        """Replay all compliance_audit_events for app_id and store a snapshot."""
        state = await self._replay_to_state(app_id, conn)
        await conn.execute(
            """
            INSERT INTO compliance_audit_snapshots
                (application_id, snapshot_at, last_global_position, state)
            VALUES ($1, $2, $3, $4)
            """,
            app_id,
            trigger_event.recorded_at,
            trigger_event.global_position,
            state.to_json(),
        )

    async def _replay_to_state(
        self,
        app_id: str,
        conn: asyncpg.Connection,
        up_to_recorded_at: datetime | None = None,
    ) -> ComplianceState:
        """Replay compliance_audit_events for app_id into a ComplianceState."""
        if up_to_recorded_at is not None:
            rows = await conn.fetch(
                """
                SELECT event_type, rule_id, rule_version,
                       regulation_set_version, result, failure_reason
                FROM compliance_audit_events
                WHERE application_id = $1 AND recorded_at <= $2
                ORDER BY id
                """,
                app_id, up_to_recorded_at,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT event_type, rule_id, rule_version,
                       regulation_set_version, result, failure_reason
                FROM compliance_audit_events
                WHERE application_id = $1
                ORDER BY id
                """,
                app_id,
            )

        state = ComplianceState(application_id=app_id)
        for row in rows:
            match row["event_type"]:
                case "ComplianceCheckRequested":
                    state.regulation_set_version = row["regulation_set_version"]
                case "ComplianceRulePassed":
                    rule_id = row["rule_id"] or ""
                    state.passed_checks[rule_id] = row["rule_version"] or ""
                    state.failed_checks.pop(rule_id, None)
                case "ComplianceRuleFailed":
                    rule_id = row["rule_id"] or ""
                    state.failed_checks[rule_id] = row["failure_reason"] or ""
                    state.passed_checks.pop(rule_id, None)
                case "ComplianceClearanceIssued":
                    state.clearance_issued = True
        return state

    # ------------------------------------------------------------------
    # Public query interface
    # ------------------------------------------------------------------

    async def get_current_compliance(
        self, application_id: str, conn: asyncpg.Connection
    ) -> ComplianceState:
        """Full compliance record with all checks, verdicts, and regulation versions."""
        return await self._replay_to_state(application_id, conn)

    async def get_compliance_at(
        self, application_id: str, timestamp: datetime, conn: asyncpg.Connection
    ) -> ComplianceState:
        """
        Compliance state as it existed at a specific moment — regulatory time-travel.

        Strategy:
        1. Find the latest snapshot taken at or before timestamp.
        2. If found, deserialise it and replay only events after the snapshot.
        3. If not found, replay all events up to timestamp from scratch.
        """
        snapshot_row = await conn.fetchrow(
            """
            SELECT state, last_global_position, snapshot_at
            FROM compliance_audit_snapshots
            WHERE application_id = $1 AND snapshot_at <= $2
            ORDER BY snapshot_at DESC
            LIMIT 1
            """,
            application_id, timestamp,
        )

        if snapshot_row:
            state = ComplianceState.from_json(snapshot_row["state"])
            # Replay only events after the snapshot up to timestamp
            rows = await conn.fetch(
                """
                SELECT event_type, rule_id, rule_version,
                       regulation_set_version, result, failure_reason
                FROM compliance_audit_events
                WHERE application_id = $1
                  AND global_position > $2
                  AND recorded_at <= $3
                ORDER BY id
                """,
                application_id,
                snapshot_row["last_global_position"],
                timestamp,
            )
            for row in rows:
                match row["event_type"]:
                    case "ComplianceCheckRequested":
                        state.regulation_set_version = row["regulation_set_version"]
                    case "ComplianceRulePassed":
                        rule_id = row["rule_id"] or ""
                        state.passed_checks[rule_id] = row["rule_version"] or ""
                        state.failed_checks.pop(rule_id, None)
                    case "ComplianceRuleFailed":
                        rule_id = row["rule_id"] or ""
                        state.failed_checks[rule_id] = row["failure_reason"] or ""
                        state.passed_checks.pop(rule_id, None)
                    case "ComplianceClearanceIssued":
                        state.clearance_issued = True
            return state

        # No snapshot — full replay up to timestamp
        return await self._replay_to_state(application_id, conn, up_to_recorded_at=timestamp)

    async def get_projection_lag(self, conn: asyncpg.Connection) -> float:
        """Milliseconds between latest event in store and latest event this projection processed."""
        return await self.get_lag(conn)

    async def rebuild(self, conn: asyncpg.Connection) -> None:
        """
        Truncate projection tables and reset checkpoint to 0.
        Live reads continue against the (now empty) tables during rebuild —
        no downtime. The daemon replays all events from position 0 on next poll.
        """
        await conn.execute("TRUNCATE TABLE compliance_audit_events")
        await conn.execute("TRUNCATE TABLE compliance_audit_snapshots")
        self._event_counts.clear()
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position)
            VALUES ($1, 0)
            ON CONFLICT (projection_name) DO UPDATE SET last_position = 0, updated_at = NOW()
            """,
            self.name,
        )

"""
MCP Resources — the Query side of CQRS.

Resources expose projections. They NEVER load aggregate streams — all reads
come from projections. A resource that replays events on every query is an
anti-pattern that will not scale.

Justified exceptions (per the brief):
- ledger://applications/{id}/audit-trail: reads AuditLedger stream directly.
  Justified because the AuditLedger is itself a projection-like append-only log
  and there is no separate projection table for it.
- ledger://agents/{id}/sessions/{session_id}: reads AgentSession stream directly.
  Justified because session replay is the Gas Town pattern — the stream IS the
  agent's memory.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.types import Resource, TextContent

from src.event_store import EventStore
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


def register_resources(
    server: Server,
    store: EventStore,
    pool,
    daemon: ProjectionDaemon,
    app_summary: ApplicationSummaryProjection,
    agent_perf: AgentPerformanceLedgerProjection,
    compliance_audit: ComplianceAuditViewProjection,
) -> None:
    """Register all 6 MCP resources on the server instance."""

    @server.list_resources()
    async def list_resources() -> list[Resource]:
        return [
            Resource(uri="ledger://applications/{id}", name="Application Summary",
                     description="Current state of a loan application. SLO: p99 < 50ms.",
                     mimeType="application/json"),
            Resource(uri="ledger://applications/{id}/compliance", name="Compliance Audit View",
                     description="Full compliance record. Supports ?as_of=timestamp for time-travel. SLO: p99 < 200ms.",
                     mimeType="application/json"),
            Resource(uri="ledger://applications/{id}/audit-trail", name="Audit Trail",
                     description="Complete audit trail for an application. Supports ?from=&to= range. SLO: p99 < 500ms.",
                     mimeType="application/json"),
            Resource(uri="ledger://agents/{id}/performance", name="Agent Performance",
                     description="Aggregated performance metrics for an agent. SLO: p99 < 50ms.",
                     mimeType="application/json"),
            Resource(uri="ledger://agents/{id}/sessions/{session_id}", name="Agent Session",
                     description="Full agent session replay. SLO: p99 < 300ms.",
                     mimeType="application/json"),
            Resource(uri="ledger://ledger/health", name="Ledger Health",
                     description="Projection lag metrics for all projections. SLO: p99 < 10ms.",
                     mimeType="application/json"),
        ]

    @server.read_resource()
    async def read_resource(uri: str) -> str:
        parts = str(uri).split("/")

        # ledger://ledger/health
        if str(uri) == "ledger://ledger/health":
            lags = daemon.get_all_lags()
            return _json({"lags_ms": lags, "status": "ok"})

        # ledger://applications/{id}
        if str(uri).startswith("ledger://applications/"):
            remainder = str(uri).removeprefix("ledger://applications/")

            # ledger://applications/{id}/compliance[?as_of=...]
            if "/compliance" in remainder:
                app_id, _, query = remainder.partition("/compliance")
                as_of = None
                if "as_of=" in query:
                    from datetime import datetime, timezone
                    ts_str = query.split("as_of=")[-1].split("&")[0]
                    as_of = datetime.fromisoformat(ts_str)
                    if as_of.tzinfo is None:
                        as_of = as_of.replace(tzinfo=timezone.utc)
                async with pool.acquire() as conn:
                    if as_of:
                        state = await compliance_audit.get_compliance_at(app_id, as_of, conn)
                    else:
                        state = await compliance_audit.get_current_compliance(app_id, conn)
                from dataclasses import asdict
                return _json(asdict(state))

            # ledger://applications/{id}/audit-trail[?from=&to=]
            if "/audit-trail" in remainder:
                app_id, _, query = remainder.partition("/audit-trail")
                from_pos = 0
                to_pos = None
                if "from=" in query:
                    from_pos = int(query.split("from=")[-1].split("&")[0])
                if "to=" in query:
                    to_pos = int(query.split("to=")[-1].split("&")[0])
                # Justified exception: direct stream read for audit trail
                events = await store.load_stream(
                    f"audit-loan-{app_id}",
                    from_position=from_pos,
                    to_position=to_pos,
                )
                return _json([{
                    "event_type": e.event_type,
                    "stream_position": e.stream_position,
                    "global_position": e.global_position,
                    "payload": e.payload,
                    "recorded_at": e.recorded_at,
                } for e in events])

            # ledger://applications/{id}
            app_id = remainder.split("?")[0]
            async with pool.acquire() as conn:
                row = await app_summary.get_current(app_id, conn)
            return _json(row or {"error": "not_found", "application_id": app_id})

        # ledger://agents/{id}/...
        if str(uri).startswith("ledger://agents/"):
            remainder = str(uri).removeprefix("ledger://agents/")

            # ledger://agents/{id}/sessions/{session_id}
            if "/sessions/" in remainder:
                agent_id, _, session_id = remainder.partition("/sessions/")
                session_id = session_id.split("?")[0]
                # Justified exception: direct stream read for Gas Town session replay
                events = await store.load_stream(f"agent-{agent_id}-{session_id}")
                return _json([{
                    "event_type": e.event_type,
                    "stream_position": e.stream_position,
                    "payload": e.payload,
                    "recorded_at": e.recorded_at,
                } for e in events])

            # ledger://agents/{id}/performance
            if "/performance" in remainder:
                agent_id = remainder.split("/performance")[0].split("?")[0]
                async with pool.acquire() as conn:
                    rows = await agent_perf.get_metrics(agent_id, None, conn)
                return _json(rows or {"error": "not_found", "agent_id": agent_id})

        return _json({"error": "unknown_resource", "uri": str(uri)})

"""
MCP Server entry point for The Ledger.

Exposes 8 tools (command side) and 6 resources (query side).
Run with: uv run python -m src.mcp.server
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server

from src.event_store import EventStore
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon
from src.mcp.tools import register_tools
from src.mcp.resources import register_resources

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://shuaib@/ledger_test?host=/var/run/postgresql",
)


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL)
    store = EventStore(pool)

    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance_audit = ComplianceAuditViewProjection()

    daemon = ProjectionDaemon(store, [app_summary, agent_perf, compliance_audit])

    server = Server("ledger")
    register_tools(server, store)
    register_resources(server, store, pool, daemon, app_summary, agent_perf, compliance_audit)

    # Start daemon in background
    daemon_task = asyncio.create_task(daemon.run_forever(poll_interval_ms=100))

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        daemon.stop()
        daemon_task.cancel()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

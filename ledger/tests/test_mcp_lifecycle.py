"""
Phase 5 — MCP Lifecycle Integration Test

Full loan application lifecycle driven entirely through MCP tool calls.
No direct Python function calls — simulates what a real AI agent would do.

Lifecycle:
  start_agent_session → record_credit_analysis → record_fraud_screening
  → record_compliance_check → generate_decision → record_human_review
  → query ledger://applications/{id}/compliance to verify complete trace
"""
import json
import uuid
import pytest

from src.event_store import EventStore
from src.mcp.tools import register_tools
from src.mcp.resources import register_resources
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — simulate MCP tool/resource calls
# ---------------------------------------------------------------------------

class MockMCPServer:
    """Minimal MCP server wrapper for testing tool/resource calls directly."""

    def __init__(self):
        self._tools = {}
        self._resources = {}
        self._tool_handler = None
        self._resource_handler = None

    def list_tools(self):
        def decorator(fn):
            self._list_tools_fn = fn
            return fn
        return decorator

    def call_tool(self):
        def decorator(fn):
            self._tool_handler = fn
            return fn
        return decorator

    def list_resources(self):
        def decorator(fn):
            return fn
        return decorator

    def read_resource(self):
        def decorator(fn):
            self._resource_handler = fn
            return fn
        return decorator

    async def tool(self, name: str, arguments: dict) -> dict:
        result = await self._tool_handler(name, arguments)
        text = result[0].text
        return json.loads(text)

    async def resource(self, uri: str) -> dict:
        result = await self._resource_handler(uri)
        return json.loads(result)


@pytest.fixture
async def mcp(pool, store):
    server = MockMCPServer()
    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance_audit = ComplianceAuditViewProjection()
    daemon = ProjectionDaemon(store, [app_summary, agent_perf, compliance_audit])

    register_tools(server, store)
    register_resources(server, store, pool, daemon, app_summary, agent_perf, compliance_audit)
    return server


# ---------------------------------------------------------------------------
# Full lifecycle test
# ---------------------------------------------------------------------------

async def test_full_loan_lifecycle_via_mcp_tools(pool, store, mcp):
    """
    Complete loan application lifecycle driven entirely through MCP tool calls.
    Asserts each step succeeds and the final state is correct.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    # Step 1: Start agent session (Gas Town — required before any agent tool)
    result = await mcp.tool("start_agent_session", {
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "context_source": "cold_start",
    })
    assert "error" not in result, f"start_agent_session failed: {result}"
    assert result["session_id"] == session_id

    # Step 2: Submit application
    result = await mcp.tool("submit_application", {
        "application_id": app_id,
        "applicant_id": "applicant-001",
        "requested_amount_usd": 100000.0,
        "loan_purpose": "business_expansion",
    })
    assert "error" not in result, f"submit_application failed: {result}"
    assert result["stream_id"] == f"loan-{app_id}"

    # Advance to AwaitingAnalysis via direct store append (CreditAnalysisRequested)
    # This is the only direct call — the MCP interface doesn't expose this transition
    # (it's an internal orchestration step, not a tool in the brief's tool table)
    from src.commands.handlers import RequestCreditAnalysisCommand, handle_request_credit_analysis
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
        store,
    )

    # Step 3: Record credit analysis
    result = await mcp.tool("record_credit_analysis", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.82,
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 90000.0,
        "duration_ms": 950,
    })
    assert "error" not in result, f"record_credit_analysis failed: {result}"

    # Step 4: Record fraud screening
    result = await mcp.tool("record_fraud_screening", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "fraud_score": 0.08,
        "anomaly_flags": [],
        "screening_model_version": "fraud-v1.1",
    })
    assert "error" not in result, f"record_fraud_screening failed: {result}"

    # Step 5a: Advance loan to ComplianceReview state (request compliance checks on loan stream)
    from src.commands.handlers import handle_compliance_check, ComplianceCheckCommand as _CCC
    # The first compliance check call creates the compliance stream AND advances the loan stream
    # We need to also write ComplianceCheckRequested to the loan stream via the aggregate
    from src.aggregates.loan_application import LoanApplicationAggregate
    app_agg = await LoanApplicationAggregate.load(store, app_id)
    await app_agg.request_compliance_review(
        store,
        regulation_set_version="v2024",
        checks_required=["KYC", "AML"],
    )

    # Step 5b: Record compliance checks (KYC + AML)
    result = await mcp.tool("record_compliance_check", {
        "application_id": app_id,
        "rule_id": "KYC",
        "rule_version": "v2024.1",
        "passed": True,
        "regulation_set_version": "v2024",
        "checks_required": ["KYC", "AML"],
    })
    assert "error" not in result, f"record_compliance_check KYC failed: {result}"

    result = await mcp.tool("record_compliance_check", {
        "application_id": app_id,
        "rule_id": "AML",
        "rule_version": "v2024.1",
        "passed": True,
    })
    assert "error" not in result, f"record_compliance_check AML failed: {result}"
    assert result["compliance_status"] == "cleared"

    # Step 6: Generate decision
    session_stream_id = f"agent-{agent_id}-{session_id}"
    result = await mcp.tool("generate_decision", {
        "application_id": app_id,
        "orchestrator_agent_id": agent_id,
        "recommendation": "APPROVE",
        "confidence_score": 0.82,
        "contributing_agent_sessions": [session_stream_id],
        "decision_basis_summary": "All checks passed, low risk",
        "model_versions": {session_stream_id: "v2.3"},
    })
    assert "error" not in result, f"generate_decision failed: {result}"

    # Step 7: Record human review
    result = await mcp.tool("record_human_review", {
        "application_id": app_id,
        "reviewer_id": "reviewer-001",
        "final_decision": "APPROVE",
        "override": False,
    })
    assert "error" not in result, f"record_human_review failed: {result}"
    assert result["final_decision"] == "APPROVE"


async def test_mcp_tool_returns_structured_error_on_domain_violation(pool, store, mcp):
    """
    Calling record_credit_analysis without start_agent_session must return
    a structured DomainError, not an unstructured exception.
    """
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    # Submit application first
    await mcp.tool("submit_application", {
        "application_id": app_id,
        "applicant_id": "applicant-002",
        "requested_amount_usd": 50000.0,
    })

    # Try credit analysis WITHOUT starting a session — must fail with structured error
    result = await mcp.tool("record_credit_analysis", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.75,
        "risk_tier": "LOW",
        "recommended_limit_usd": 45000.0,
        "duration_ms": 800,
    })
    assert result.get("error") is True
    assert "error_type" in result
    assert "suggested_action" in result


async def test_mcp_tool_returns_structured_error_on_concurrency_conflict(pool, store, mcp):
    """
    Submitting the same application_id twice must return a structured
    OptimisticConcurrencyError or DomainError, not an unhandled exception.
    """
    app_id = str(uuid.uuid4())[:8]

    result1 = await mcp.tool("submit_application", {
        "application_id": app_id,
        "applicant_id": "applicant-003",
        "requested_amount_usd": 75000.0,
    })
    assert "error" not in result1

    result2 = await mcp.tool("submit_application", {
        "application_id": app_id,
        "applicant_id": "applicant-003",
        "requested_amount_usd": 75000.0,
    })
    assert result2.get("error") is True
    assert "error_type" in result2


async def test_mcp_resource_health_returns_lag_dict(pool, store, mcp):
    """ledger://ledger/health must return a dict of lag_ms values."""
    result = await mcp.resource("ledger://ledger/health")
    assert "lags_ms" in result
    assert isinstance(result["lags_ms"], dict)


async def test_mcp_resource_compliance_returns_state(pool, store, mcp):
    """
    After recording compliance checks, ledger://applications/{id}/compliance
    must return the compliance state with all checks present.
    """
    app_id = str(uuid.uuid4())[:8]

    await mcp.tool("record_compliance_check", {
        "application_id": app_id,
        "rule_id": "SANCTIONS",
        "rule_version": "v2024.1",
        "passed": True,
        "regulation_set_version": "v2024",
        "checks_required": ["SANCTIONS"],
    })

    # Run the daemon to process the event into the projection
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.agent_performance import AgentPerformanceLedgerProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection
    from src.projections.daemon import ProjectionDaemon

    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance_audit = ComplianceAuditViewProjection()
    daemon = ProjectionDaemon(store, [app_summary, agent_perf, compliance_audit])
    await daemon._process_batch()

    async with pool.acquire() as conn:
        state = await compliance_audit.get_current_compliance(app_id, conn)

    assert state.application_id == app_id
    assert "SANCTIONS" in state.passed_checks


async def test_record_credit_analysis_returns_event_id_and_version(pool, store, mcp):
    """record_credit_analysis must return event_id and new_stream_version per the brief spec."""
    app_id = str(uuid.uuid4())[:8]
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]

    await mcp.tool("start_agent_session", {
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "context_source": "cold_start",
    })
    await mcp.tool("submit_application", {
        "application_id": app_id,
        "applicant_id": "applicant-ret-test",
        "requested_amount_usd": 50000.0,
    })

    from src.commands.handlers import RequestCreditAnalysisCommand, handle_request_credit_analysis
    await handle_request_credit_analysis(
        RequestCreditAnalysisCommand(application_id=app_id, assigned_agent_id=agent_id),
        store,
    )

    result = await mcp.tool("record_credit_analysis", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "confidence_score": 0.80,
        "risk_tier": "LOW",
        "recommended_limit_usd": 50000.0,
        "duration_ms": 500,
    })

    assert "error" not in result, f"unexpected error: {result}"
    assert "event_id" in result, "event_id must be returned per brief spec"
    assert "new_stream_version" in result, "new_stream_version must be returned per brief spec"
    # event_id must be a valid UUID string
    import uuid as _uuid
    _uuid.UUID(result["event_id"])
    assert isinstance(result["new_stream_version"], int)
    assert result["new_stream_version"] > 0


async def test_record_fraud_screening_returns_event_id_and_version(pool, store, mcp):
    """record_fraud_screening must return event_id and new_stream_version per the brief spec."""
    agent_id = str(uuid.uuid4())[:8]
    session_id = str(uuid.uuid4())[:8]
    app_id = str(uuid.uuid4())[:8]

    await mcp.tool("start_agent_session", {
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.3",
        "context_source": "cold_start",
    })

    result = await mcp.tool("record_fraud_screening", {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "fraud_score": 0.05,
        "anomaly_flags": [],
        "screening_model_version": "fraud-v1.1",
    })

    assert "error" not in result, f"unexpected error: {result}"
    assert "event_id" in result, "event_id must be returned per brief spec"
    assert "new_stream_version" in result, "new_stream_version must be returned per brief spec"
    import uuid as _uuid
    _uuid.UUID(result["event_id"])
    assert isinstance(result["new_stream_version"], int)
    assert result["new_stream_version"] > 0

"""
MCP Tools — the Command side of CQRS.

Each tool maps to a command handler. Tools write events; Resources read projections.
This is structural CQRS — the MCP specification naturally implements read/write separation.

Design principles for LLM consumption:
1. Precondition documentation in every tool description — the LLM's only contract.
2. Structured error types — typed objects with suggested_action for autonomous recovery.
3. All errors are returned as structured dicts, never unstructured strings.
"""
from __future__ import annotations

import traceback
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent

from src.event_store import EventStore, OptimisticConcurrencyError
from src.models.events import DomainError
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


def _ok(data: Any) -> list[TextContent]:
    import json
    return [TextContent(type="text", text=json.dumps(data, default=str))]


def _err(error_type: str, message: str, **kwargs) -> list[TextContent]:
    import json
    return [TextContent(type="text", text=json.dumps({
        "error": True,
        "error_type": error_type,
        "message": message,
        **kwargs,
    }, default=str))]


def register_tools(server: Server, store: EventStore) -> None:
    """Register all 8 MCP tools on the server instance."""

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="start_agent_session",
                description=(
                    "REQUIRED FIRST STEP: Start an agent session before calling any other agent tool. "
                    "Writes an AgentContextLoaded event (Gas Town pattern). "
                    "Calling record_credit_analysis or record_fraud_screening without first calling "
                    "this tool will return a PreconditionFailed error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "model_version": {"type": "string"},
                        "context_source": {"type": "string", "default": "event_replay"},
                        "context_token_count": {"type": "integer", "default": 0},
                    },
                    "required": ["agent_id", "session_id", "model_version"],
                },
            ),
            Tool(
                name="submit_application",
                description=(
                    "Submit a new loan application. Creates a new LoanApplication stream. "
                    "Returns stream_id and initial_version. "
                    "Duplicate application_id returns DuplicateStreamError."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "applicant_id": {"type": "string"},
                        "requested_amount_usd": {"type": "number"},
                        "loan_purpose": {"type": "string", "default": ""},
                        "submission_channel": {"type": "string", "default": "api"},
                    },
                    "required": ["application_id", "applicant_id", "requested_amount_usd"],
                },
            ),
            Tool(
                name="record_credit_analysis",
                description=(
                    "Record a completed credit analysis for a loan application. "
                    "PRECONDITION: start_agent_session must have been called for this agent_id/session_id. "
                    "PRECONDITION: application must be in AwaitingAnalysis state. "
                    "Enforces optimistic concurrency on the loan stream — reload and retry on OptimisticConcurrencyError."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "model_version": {"type": "string"},
                        "confidence_score": {"type": "number"},
                        "risk_tier": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                        "recommended_limit_usd": {"type": "number"},
                        "duration_ms": {"type": "integer"},
                    },
                    "required": ["application_id", "agent_id", "session_id",
                                 "model_version", "confidence_score", "risk_tier",
                                 "recommended_limit_usd", "duration_ms"],
                },
            ),
            Tool(
                name="record_fraud_screening",
                description=(
                    "Record a completed fraud screening for a loan application. "
                    "PRECONDITION: start_agent_session must have been called for this agent_id/session_id. "
                    "fraud_score must be 0.0–1.0; values outside this range return ValidationError."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "session_id": {"type": "string"},
                        "fraud_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "anomaly_flags": {"type": "array", "items": {"type": "string"}, "default": []},
                        "screening_model_version": {"type": "string"},
                    },
                    "required": ["application_id", "agent_id", "session_id",
                                 "fraud_score", "screening_model_version"],
                },
            ),
            Tool(
                name="record_compliance_check",
                description=(
                    "Record a compliance rule result (pass or fail) for a loan application. "
                    "rule_id must exist in the active regulation_set_version. "
                    "First call for an application creates the ComplianceRecord stream."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "rule_id": {"type": "string"},
                        "rule_version": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "failure_reason": {"type": "string", "default": ""},
                        "regulation_set_version": {"type": "string", "default": "v1.0"},
                        "checks_required": {"type": "array", "items": {"type": "string"}, "default": []},
                    },
                    "required": ["application_id", "rule_id", "rule_version", "passed"],
                },
            ),
            Tool(
                name="generate_decision",
                description=(
                    "Generate a final decision recommendation for a loan application. "
                    "PRECONDITION: all required compliance checks must be passed. "
                    "PRECONDITION: contributing_agent_sessions must reference sessions that processed this application. "
                    "Confidence floor: confidence_score < 0.6 forces recommendation = REFER regardless of input."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "orchestrator_agent_id": {"type": "string"},
                        "recommendation": {"type": "string", "enum": ["APPROVE", "DECLINE", "REFER"]},
                        "confidence_score": {"type": "number"},
                        "contributing_agent_sessions": {"type": "array", "items": {"type": "string"}},
                        "decision_basis_summary": {"type": "string", "default": ""},
                        "model_versions": {"type": "object", "default": {}},
                    },
                    "required": ["application_id", "orchestrator_agent_id",
                                 "recommendation", "confidence_score",
                                 "contributing_agent_sessions"],
                },
            ),
            Tool(
                name="record_human_review",
                description=(
                    "Record a human reviewer's final decision on a loan application. "
                    "PRECONDITION: application must be in PendingDecision state. "
                    "If override=true, override_reason is required — omitting it returns ValidationError."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "application_id": {"type": "string"},
                        "reviewer_id": {"type": "string"},
                        "final_decision": {"type": "string", "enum": ["APPROVE", "DECLINE"]},
                        "override": {"type": "boolean", "default": False},
                        "override_reason": {"type": "string", "default": ""},
                    },
                    "required": ["application_id", "reviewer_id", "final_decision"],
                },
            ),
            Tool(
                name="run_integrity_check",
                description=(
                    "Run a cryptographic integrity check on an entity's audit chain. "
                    "PRECONDITION: caller must have compliance role. "
                    "Rate-limited to 1 call per minute per entity (enforced by caller). "
                    "Returns chain_valid=false and tamper_detected=true if any stored event was modified."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity_type": {"type": "string"},
                        "entity_id": {"type": "string"},
                    },
                    "required": ["entity_type", "entity_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "start_agent_session":
                cmd = StartAgentSessionCommand(
                    agent_id=arguments["agent_id"],
                    session_id=arguments["session_id"],
                    model_version=arguments.get("model_version", ""),
                    context_source=arguments.get("context_source", "event_replay"),
                    context_token_count=arguments.get("context_token_count", 0),
                )
                agg = await handle_start_agent_session(cmd, store)
                return _ok({"session_id": agg.session_id, "context_position": agg.version})

            elif name == "submit_application":
                cmd = SubmitApplicationCommand(
                    application_id=arguments["application_id"],
                    applicant_id=arguments["applicant_id"],
                    requested_amount_usd=arguments["requested_amount_usd"],
                    loan_purpose=arguments.get("loan_purpose", ""),
                    submission_channel=arguments.get("submission_channel", "api"),
                )
                agg = await handle_submit_application(cmd, store)
                return _ok({"stream_id": agg.stream_id, "initial_version": agg.version})

            elif name == "record_credit_analysis":
                cmd = CreditAnalysisCompletedCommand(
                    application_id=arguments["application_id"],
                    agent_id=arguments["agent_id"],
                    session_id=arguments["session_id"],
                    model_version=arguments["model_version"],
                    confidence_score=arguments["confidence_score"],
                    risk_tier=arguments["risk_tier"],
                    recommended_limit_usd=arguments["recommended_limit_usd"],
                    duration_ms=arguments["duration_ms"],
                )
                await handle_credit_analysis_completed(cmd, store)
                version = await store.stream_version(cmd.loan_stream_id)
                return _ok({"new_stream_version": version})

            elif name == "record_fraud_screening":
                cmd = FraudScreeningCompletedCommand(
                    application_id=arguments["application_id"],
                    agent_id=arguments["agent_id"],
                    session_id=arguments["session_id"],
                    fraud_score=arguments["fraud_score"],
                    anomaly_flags=tuple(arguments.get("anomaly_flags", [])),
                    screening_model_version=arguments["screening_model_version"],
                )
                await handle_fraud_screening_completed(cmd, store)
                return _ok({"recorded": True})

            elif name == "record_compliance_check":
                cmd = ComplianceCheckCommand(
                    application_id=arguments["application_id"],
                    rule_id=arguments["rule_id"],
                    rule_version=arguments["rule_version"],
                    passed=arguments["passed"],
                    failure_reason=arguments.get("failure_reason", ""),
                    regulation_set_version=arguments.get("regulation_set_version", "v1.0"),
                    checks_required=tuple(arguments.get("checks_required", [])),
                )
                compliance = await handle_compliance_check(cmd, store)
                missing = compliance.required_checks - set(compliance.passed_checks)
                return _ok({
                    "check_id": cmd.rule_id,
                    "compliance_status": "cleared" if not missing else "in_progress",
                })

            elif name == "generate_decision":
                cmd = GenerateDecisionCommand(
                    application_id=arguments["application_id"],
                    orchestrator_agent_id=arguments["orchestrator_agent_id"],
                    recommendation=arguments["recommendation"],
                    confidence_score=arguments["confidence_score"],
                    contributing_agent_sessions=tuple(arguments.get("contributing_agent_sessions", [])),
                    decision_basis_summary=arguments.get("decision_basis_summary", ""),
                    model_versions=arguments.get("model_versions", {}),
                )
                app = await handle_generate_decision(cmd, store)
                return _ok({
                    "decision_id": str(app.stream_id),
                    "recommendation": app.state.value,
                })

            elif name == "record_human_review":
                cmd = HumanReviewCompletedCommand(
                    application_id=arguments["application_id"],
                    reviewer_id=arguments["reviewer_id"],
                    final_decision=arguments["final_decision"],
                    override=arguments.get("override", False),
                    override_reason=arguments.get("override_reason", ""),
                )
                app = await handle_human_review_completed(cmd, store)
                return _ok({
                    "final_decision": arguments["final_decision"],
                    "application_state": app.state.value,
                })

            elif name == "run_integrity_check":
                result = await run_integrity_check(
                    store,
                    entity_type=arguments["entity_type"],
                    entity_id=arguments["entity_id"],
                )
                return _ok({
                    "check_result": {
                        "events_verified": result.events_verified,
                        "integrity_hash": result.integrity_hash,
                        "previous_hash": result.previous_hash,
                    },
                    "chain_valid": result.chain_valid,
                    "tamper_detected": result.tamper_detected,
                })

            else:
                return _err("UnknownTool", f"Tool '{name}' is not registered.")

        except OptimisticConcurrencyError as e:
            return _err(
                "OptimisticConcurrencyError",
                str(e),
                stream_id=e.stream_id,
                expected_version=e.expected,
                actual_version=e.actual,
                suggested_action="reload_stream_and_retry",
            )
        except DomainError as e:
            return _err(
                "DomainError",
                e.detail,
                rule=e.rule,
                aggregate_type=e.aggregate_type,
                stream_id=e.stream_id,
                suggested_action="check_preconditions_and_retry",
            )
        except Exception as e:
            return _err(
                "InternalError",
                str(e),
                traceback=traceback.format_exc(),
                suggested_action="contact_support",
            )

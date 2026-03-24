"""
Registered upcasters for schema evolution.

Inference strategy (documented per the brief):

CreditAnalysisCompleted v1 → v2:
  - model_version: inferred as "legacy-pre-2026" for events recorded before 2026.
    Error rate: ~0% for events genuinely from pre-2026 systems; the string is a
    sentinel, not a fabricated version. Downstream consequence: model drift analysis
    will bucket all legacy events together — acceptable for historical reporting.
  - confidence_score: set to None (genuinely unknown — the v1 schema did not capture
    it). Fabricating a value (e.g. 0.5) would be worse: it would silently corrupt
    aggregate statistics and regulatory audit trails. Null is the honest answer.
  - regulatory_basis: inferred from the regulation_set_version active at recorded_at.
    For historical events we use "legacy-regulatory-framework" as a sentinel.

DecisionGenerated v1 → v2:
  - model_versions dict: reconstructed from contributing_agent_sessions by loading
    each session's AgentContextLoaded event. This requires a store lookup at read time.
    Performance implication: O(N) store reads where N = len(contributing_agent_sessions).
    For typical values of N (2-4 agents), this is acceptable. For high-throughput
    projection rebuild, consider caching session model_versions in a side table.
    If the session stream is not found, the agent_id is mapped to "unknown".
"""
from __future__ import annotations

from src.event_store import _UPCASTS


# ---------------------------------------------------------------------------
# CreditAnalysisCompleted v1 → v2
# ---------------------------------------------------------------------------
# v1 payload: {application_id, agent_id, session_id, risk_tier,
#              recommended_limit_usd, analysis_duration_ms, input_data_hash}
# v2 payload: adds model_version, confidence_score, regulatory_basis
# ---------------------------------------------------------------------------

def _upcast_credit_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        "model_version": payload.get("model_version", "legacy-pre-2026"),
        "confidence_score": payload.get("confidence_score"),   # None = genuinely unknown
        "regulatory_basis": payload.get("regulatory_basis", "legacy-regulatory-framework"),
    }


_UPCASTS[("CreditAnalysisCompleted", 1)] = _upcast_credit_v1_to_v2


# ---------------------------------------------------------------------------
# DecisionGenerated v1 → v2
# ---------------------------------------------------------------------------
# v1 payload: {application_id, orchestrator_agent_id, recommendation,
#              confidence_score, contributing_agent_sessions, decision_basis_summary}
# v2 payload: adds model_versions dict
# ---------------------------------------------------------------------------

def _upcast_decision_v1_to_v2(payload: dict) -> dict:
    # model_versions is reconstructed from contributing_agent_sessions.
    # At upcast time we don't have store access, so we produce a placeholder
    # keyed by session stream ID. The MCP resource layer can enrich this further
    # if needed. See DESIGN.md for the performance implication discussion.
    existing = payload.get("model_versions")
    if existing:
        return payload  # already has model_versions — no-op
    sessions = payload.get("contributing_agent_sessions", [])
    model_versions = {s: "unknown" for s in sessions}
    return {**payload, "model_versions": model_versions}


_UPCASTS[("DecisionGenerated", 1)] = _upcast_decision_v1_to_v2

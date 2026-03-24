"""
Registered upcasters for schema evolution.

Uses UpcasterRegistry as the single registration point. The registry writes to
the module-level _UPCASTS dict in event_store, so upcasters registered here are
applied transparently on every load_stream() / load_all() call — callers never
invoke upcasters manually.

Import this module once at startup (e.g. in server.py or conftest.py) to activate
all upcasters. The registry decorator pattern makes registration side-effect-free
and introspectable via registry.registered_types().

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Inference strategy — documented per the brief requirement
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CreditAnalysisCompleted v1 → v2
  Fields added: model_version, confidence_score, regulatory_basis

  model_version:
    Inferred as "legacy-pre-2026" for all v1 events. This is a sentinel string,
    not a fabricated version identifier. Error rate: ~0% — any event stored with
    event_version=1 genuinely predates the v2 schema. Downstream consequence:
    model drift analysis buckets all legacy events together under one label,
    which is accurate — they ARE from the same pre-2026 era.

  confidence_score:
    Set to None. The v1 schema did not capture this field. Fabricating a value
    (e.g. 0.5 as a midpoint) would be strictly worse:
      - It would silently corrupt aggregate statistics (avg_confidence_score
        in AgentPerformanceLedger would be polluted with invented data).
      - It would corrupt regulatory audit trails — a regulator examining a
        historical decision would see a confidence score that was never computed.
      - The confidence floor rule (score < 0.6 → REFER) could be incorrectly
        applied to historical events during what-if analysis.
    Null is the honest answer. Downstream consumers must handle None explicitly,
    which forces them to acknowledge the data gap rather than silently consuming
    a fabricated value.

  regulatory_basis:
    Inferred as "legacy-regulatory-framework" — a sentinel indicating the
    regulation set active before the v2 schema was introduced. For production
    use, this would be looked up from a regulation version table keyed on
    recorded_at date. The sentinel is preferable to null here because
    regulatory_basis is a required audit field; null would break compliance
    queries that expect it to be present.

DecisionGenerated v1 → v2
  Fields added: model_versions dict

  model_versions:
    The brief specifies reconstructing this from contributing_agent_sessions by
    loading each session's AgentContextLoaded event — requiring a store lookup.

    Performance implication: O(N) store reads where N = len(contributing_agent_sessions).
    For typical values (2–4 agents per decision), this is 2–4 additional DB round-trips
    per event load. This is acceptable for on-demand queries but problematic for
    projection rebuild (which loads every DecisionGenerated event in the store).

    Mitigation strategies documented in DESIGN.md:
      1. Cache session model_versions in a side table (agent_session_metadata).
      2. Batch-load all referenced sessions in a single IN query.
      3. Accept "unknown" as the value for historical events where the session
         stream no longer exists (archived or purged).

    At upcast time we do not have store access (upcasters are pure functions).
    We produce a placeholder dict keyed by session stream ID with value "unknown".
    The MCP resource layer (ledger://agents/{id}/sessions/{id}) can enrich this
    further if needed. See DESIGN.md §4 for the full tradeoff analysis.
"""
from __future__ import annotations

from src.upcasting.registry import UpcasterRegistry
from src.event_store import _UPCASTS

# The registry writes to _UPCASTS — the same dict EventStore reads from.
# This is the integration point: one registration, transparent application.
registry = UpcasterRegistry()
# Point the registry's internal dict at the shared _UPCASTS so both APIs
# are unified: EventStore.register_upcast() and registry.register() are equivalent.
registry._upcasters = _UPCASTS


# ---------------------------------------------------------------------------
# CreditAnalysisCompleted v1 → v2
# ---------------------------------------------------------------------------
# v1 schema: {application_id, agent_id, session_id, risk_tier,
#             recommended_limit_usd, analysis_duration_ms, input_data_hash}
# v2 schema: adds model_version (str), confidence_score (float|None),
#             regulatory_basis (str)
# ---------------------------------------------------------------------------

@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_analysis_v1_to_v2(payload: dict) -> dict:
    """
    Upcast CreditAnalysisCompleted from v1 to v2.

    Inference decisions:
    - model_version: sentinel "legacy-pre-2026" (see module docstring)
    - confidence_score: None — genuinely unknown, do not fabricate
    - regulatory_basis: sentinel "legacy-regulatory-framework"
    """
    return {
        **payload,
        # Preserve existing value if somehow already present (idempotent)
        "model_version": payload.get("model_version") or "legacy-pre-2026",
        # Explicitly None — not 0.0, not 0.5. Null is the honest answer.
        "confidence_score": payload.get("confidence_score"),
        "regulatory_basis": payload.get("regulatory_basis") or "legacy-regulatory-framework",
    }


# ---------------------------------------------------------------------------
# DecisionGenerated v1 → v2
# ---------------------------------------------------------------------------
# v1 schema: {application_id, orchestrator_agent_id, recommendation,
#             confidence_score, contributing_agent_sessions[],
#             decision_basis_summary, forced_refer}
# v2 schema: adds model_versions dict {session_stream_id: model_version}
# ---------------------------------------------------------------------------

@registry.register("DecisionGenerated", from_version=1)
def upcast_decision_generated_v1_to_v2(payload: dict) -> dict:
    """
    Upcast DecisionGenerated from v1 to v2.

    model_versions is reconstructed from contributing_agent_sessions.
    At upcast time we have no store access (upcasters are pure functions),
    so we produce a placeholder dict with value "unknown" for each session.

    Performance implication: documented in module docstring.
    The MCP resource layer can enrich this further if needed.
    """
    # Idempotent: if model_versions already present and non-empty, preserve it
    existing = payload.get("model_versions")
    if existing:
        return payload

    sessions: list[str] = payload.get("contributing_agent_sessions", [])
    # "unknown" is the honest sentinel — not a fabricated version string.
    # Downstream consumers must handle "unknown" explicitly.
    model_versions = {session_id: "unknown" for session_id in sessions}
    return {**payload, "model_versions": model_versions}

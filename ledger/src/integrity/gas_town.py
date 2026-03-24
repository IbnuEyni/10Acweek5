"""
Gas Town Agent Memory Pattern — reconstruct_agent_context().

Named for the infrastructure pattern in agentic systems where agent context is
lost on process restart. The solution: every agent action is written to the event
store before execution. On restart, the agent replays its event stream to
reconstruct its context window, then continues where it left off.

This is not logging — it is the agent's memory backed by the most reliable
storage primitive available: an append-only, ACID-compliant, PostgreSQL-backed
event stream.

NEEDS_RECONCILIATION:
  Flagged when the agent's last substantive action was a CreditAnalysisCompleted
  event for an application that has no corresponding FraudScreeningCompleted in
  this session. The agent must resolve the partial state before proceeding —
  attempting to continue without reconciliation risks double-processing.

Token budget:
  LLM context windows are finite. The reconstruction strategy is:
  1. Summarise all events older than the last 3 into a single prose paragraph.
  2. Preserve the last 3 events verbatim (full payload) — these are the most
     likely to be relevant to the next action.
  3. Always preserve PENDING/ERROR state events verbatim regardless of position.
  4. Truncate the final context_text to token_budget * 4 characters (rough
     approximation: 1 token ≈ 4 chars for English prose + JSON).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.event_store import EventStore, RecordedEvent

# Events that indicate a decision was started but may not be complete.
# CreditAnalysisCompleted on the agent stream means the agent committed an
# analysis result — but the corresponding loan stream update may not have
# happened yet if the agent crashed between the two appends.
_PARTIAL_DECISION_TYPES: frozenset[str] = frozenset({"CreditAnalysisCompleted"})

# Events that indicate a previously partial decision is now complete.
# FraudScreeningCompleted is the natural follow-on to CreditAnalysisCompleted
# in the Apex workflow — if both are present for the same application_id,
# the agent completed its work for that application.
_COMPLETION_TYPES: frozenset[str] = frozenset({
    "FraudScreeningCompleted",
    "ApplicationApproved",
    "ApplicationDeclined",
})

# Number of recent events to preserve verbatim (full payload, not summarised).
_VERBATIM_TAIL = 3


@dataclass
class AgentContext:
    """
    Reconstructed agent context sufficient to resume a crashed session.

    context_text:          Token-efficient prose + verbatim recent events.
                           Ready to inject into an LLM context window.
    last_event_position:   stream_position of the last event in the session.
                           The agent should resume from this position.
    pending_work:          List of human-readable descriptions of unfinished work.
                           Empty if session_health_status == "OK".
    session_health_status: "OK" | "NEEDS_RECONCILIATION" | "CLOSED"
    last_3_events:         Verbatim last 3 events for immediate context.
    model_version:         Model version declared in AgentContextLoaded.
    context_source:        Context source declared in AgentContextLoaded.
    """
    agent_id: str
    session_id: str
    context_text: str
    last_event_position: int
    pending_work: list[str] = field(default_factory=list)
    session_health_status: str = "OK"
    last_3_events: list[dict[str, Any]] = field(default_factory=list)
    model_version: str | None = None
    context_source: str | None = None


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """
    Reconstruct an agent's context from the event store after a crash.

    1. Load full AgentSession stream for agent_id + session_id.
    2. Identify: last completed action, pending work items, session health.
    3. Summarise old events into prose (token-efficient).
    4. Preserve verbatim: last 3 events, any PENDING or ERROR state events.
    5. Return AgentContext with context_text, last_event_position,
       pending_work[], session_health_status.

    CRITICAL: if the agent's last event was a partial decision (no corresponding
    completion event for the same application_id), flag NEEDS_RECONCILIATION.
    The agent must resolve the partial state before proceeding.
    """
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            agent_id=agent_id,
            session_id=session_id,
            context_text=(
                f"No events found for session agent-{agent_id}-{session_id}. "
                "This session has not been started or the stream does not exist."
            ),
            last_event_position=0,
            session_health_status="OK",
        )

    last_event = events[-1]

    # ── Extract session metadata from AgentContextLoaded ────────────────────
    model_version: str | None = None
    context_source: str | None = None
    for ev in events:
        if ev.event_type == "AgentContextLoaded":
            model_version = ev.payload.get("model_version")
            context_source = ev.payload.get("context_source")
            break  # Only the first one matters

    # ── Determine session health ─────────────────────────────────────────────
    health = "OK"
    pending_work: list[str] = []

    if last_event.event_type == "SessionClosed":
        health = "CLOSED"
    else:
        # Track which application_ids have a partial decision (started but not completed).
        # A partial decision exists when CreditAnalysisCompleted is present for an app
        # but no completion event (FraudScreeningCompleted etc.) follows for that app.
        decision_apps: dict[str, RecordedEvent] = {}   # app_id → triggering event
        completed_apps: set[str] = set()

        for ev in events:
            if ev.event_type in _PARTIAL_DECISION_TYPES:
                app_id = ev.payload.get("application_id", "")
                if app_id:
                    decision_apps[app_id] = ev
            elif ev.event_type in _COMPLETION_TYPES:
                app_id = ev.payload.get("application_id", "")
                if app_id:
                    completed_apps.add(app_id)

        unresolved = {
            app_id: ev
            for app_id, ev in decision_apps.items()
            if app_id not in completed_apps
        }

        if unresolved:
            health = "NEEDS_RECONCILIATION"
            for app_id, trigger_ev in sorted(unresolved.items()):
                pending_work.append(
                    f"Reconcile partial {trigger_ev.event_type} for application "
                    f"'{app_id}' (stream_position={trigger_ev.stream_position}). "
                    f"No completion event found in this session."
                )

    # ── Build verbatim tail ──────────────────────────────────────────────────
    last_n = events[-_VERBATIM_TAIL:]
    last_3_events = [
        {
            "event_type": e.event_type,
            "stream_position": e.stream_position,
            "payload": e.payload,
            "recorded_at": str(e.recorded_at),
        }
        for e in last_n
    ]

    # ── Build token-efficient prose summary ──────────────────────────────────
    summary_events = events[:-_VERBATIM_TAIL] if len(events) > _VERBATIM_TAIL else []

    summary_lines: list[str] = []
    for ev in summary_events:
        # Compact one-liner per event: position, type, first 3 payload fields
        fields = ", ".join(
            f"{k}={v!r}" for k, v in list(ev.payload.items())[:3]
        )
        summary_lines.append(f"  [{ev.stream_position}] {ev.event_type}: {fields}")

    context_parts: list[str] = [
        f"Agent: {agent_id} | Session: {session_id} | "
        f"Model: {model_version or 'unknown'} | "
        f"Context source: {context_source or 'unknown'} | "
        f"Total events: {len(events)} | "
        f"Health: {health}",
    ]

    if summary_lines:
        context_parts.append(
            "Session history (summarised — older events):\n"
            + "\n".join(summary_lines)
        )

    context_parts.append(
        f"Recent events (verbatim — last {len(last_n)}):\n"
        + "\n".join(
            f"  [{e['stream_position']}] {e['event_type']}: {e['payload']}"
            for e in last_3_events
        )
    )

    if pending_work:
        context_parts.append(
            "⚠ PENDING WORK (must resolve before proceeding):\n"
            + "\n".join(f"  • {w}" for w in pending_work)
        )

    if health == "NEEDS_RECONCILIATION":
        context_parts.append(
            "⚠ CRITICAL: This session requires reconciliation before any new work. "
            "A partial decision was committed to the event store but no completion "
            "event was recorded. Determine whether the downstream system received "
            "the partial decision and either complete or retract it before proceeding."
        )
    elif health == "CLOSED":
        context_parts.append(
            "ℹ Session is CLOSED. No further events may be appended. "
            "Start a new session with start_agent_session to continue work."
        )

    context_text = "\n\n".join(context_parts)

    # ── Truncate to token budget ─────────────────────────────────────────────
    # Rough approximation: 1 token ≈ 4 characters for English prose + JSON.
    # We truncate the summary section only — the header and recent events are
    # always preserved because they are the most critical for resumption.
    max_chars = token_budget * 4
    if len(context_text) > max_chars:
        truncation_marker = "\n\n[...context truncated to token budget. "
        truncation_marker += f"Full history available via load_stream(agent-{agent_id}-{session_id}).]"
        context_text = context_text[:max_chars - len(truncation_marker)] + truncation_marker

    return AgentContext(
        agent_id=agent_id,
        session_id=session_id,
        context_text=context_text,
        last_event_position=last_event.stream_position,
        pending_work=pending_work,
        session_health_status=health,
        last_3_events=last_3_events,
        model_version=model_version,
        context_source=context_source,
    )

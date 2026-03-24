from __future__ import annotations

import asyncpg

from src.event_store import RecordedEvent
from src.projections.base import Projection


class AgentPerformanceLedgerProjection(Projection):
    """
    Aggregated performance metrics per AI agent model version.

    Answers: "Has agent v2.3 been making systematically different decisions
    than v2.2?" — without replaying event streams.

    SLO: lag < 500ms. One row per (agent_id, model_version).

    Tracks:
    - analyses_completed, avg_confidence_score, avg_duration_ms
    - approve_rate, decline_rate, refer_rate (derived from counts)
    - human_override_rate (HumanReviewCompleted with override=True)
    """

    name = "AgentPerformanceLedger"

    event_types = [
        "AgentContextLoaded",
        "CreditAnalysisCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    ]

    async def handle(self, event: RecordedEvent, conn: asyncpg.Connection) -> None:
        p = event.payload

        match event.event_type:
            case "AgentContextLoaded":
                # Ensure a row exists for this agent+model_version on first seen.
                agent_id = p.get("agent_id", "")
                model_version = p.get("model_version", "unknown")
                if not agent_id:
                    return
                await conn.execute(
                    """
                    INSERT INTO agent_performance_ledger
                        (agent_id, model_version, first_seen_at, last_seen_at)
                    VALUES ($1, $2, $3, $3)
                    ON CONFLICT (agent_id, model_version) DO UPDATE SET
                        last_seen_at = GREATEST(
                            agent_performance_ledger.last_seen_at, EXCLUDED.last_seen_at
                        )
                    """,
                    agent_id, model_version, event.recorded_at,
                )

            case "CreditAnalysisCompleted":
                # Only count events from agent session streams (not loan stream copies)
                if not event.stream_id.startswith("agent-"):
                    return
                agent_id = p.get("agent_id", "")
                model_version = p.get("model_version", "unknown")
                confidence = p.get("confidence_score", 0.0) or 0.0
                duration = p.get("analysis_duration_ms", 0) or 0
                if not agent_id:
                    return
                await conn.execute(
                    """
                    INSERT INTO agent_performance_ledger
                        (agent_id, model_version, analyses_completed,
                         total_confidence_score, total_duration_ms,
                         first_seen_at, last_seen_at)
                    VALUES ($1, $2, 1, $3, $4, $5, $5)
                    ON CONFLICT (agent_id, model_version) DO UPDATE SET
                        analyses_completed = agent_performance_ledger.analyses_completed + 1,
                        total_confidence_score = agent_performance_ledger.total_confidence_score + $3,
                        total_duration_ms = agent_performance_ledger.total_duration_ms + $4,
                        last_seen_at = GREATEST(
                            agent_performance_ledger.last_seen_at, EXCLUDED.last_seen_at
                        )
                    """,
                    agent_id, model_version,
                    float(confidence), int(duration),
                    event.recorded_at,
                )

            case "DecisionGenerated":
                # DecisionGenerated on the loan stream — orchestrator agent
                agent_id = p.get("orchestrator_agent_id", "")
                recommendation = p.get("recommendation", "")
                if not agent_id:
                    return
                approve_inc = 1 if recommendation == "APPROVE" else 0
                decline_inc = 1 if recommendation == "DECLINE" else 0
                refer_inc   = 1 if recommendation == "REFER"   else 0
                await conn.execute(
                    """
                    INSERT INTO agent_performance_ledger
                        (agent_id, model_version, decisions_generated,
                         approve_count, decline_count, refer_count,
                         first_seen_at, last_seen_at)
                    VALUES ($1, 'orchestrator', 1, $2, $3, $4, $5, $5)
                    ON CONFLICT (agent_id, model_version) DO UPDATE SET
                        decisions_generated = agent_performance_ledger.decisions_generated + 1,
                        approve_count = agent_performance_ledger.approve_count + $2,
                        decline_count = agent_performance_ledger.decline_count + $3,
                        refer_count   = agent_performance_ledger.refer_count   + $4,
                        last_seen_at  = GREATEST(
                            agent_performance_ledger.last_seen_at, EXCLUDED.last_seen_at
                        )
                    """,
                    agent_id,
                    approve_inc, decline_inc, refer_inc,
                    event.recorded_at,
                )

            case "HumanReviewCompleted":
                # Track override rate — human overriding the AI recommendation
                reviewer_id = p.get("reviewer_id", "")
                override = p.get("override", False)
                if not reviewer_id or not override:
                    return
                await conn.execute(
                    """
                    INSERT INTO agent_performance_ledger
                        (agent_id, model_version, human_override_count,
                         first_seen_at, last_seen_at)
                    VALUES ($1, 'human_reviewer', 1, $2, $2)
                    ON CONFLICT (agent_id, model_version) DO UPDATE SET
                        human_override_count = agent_performance_ledger.human_override_count + 1,
                        last_seen_at = GREATEST(
                            agent_performance_ledger.last_seen_at, EXCLUDED.last_seen_at
                        )
                    """,
                    reviewer_id, event.recorded_at,
                )

    async def rebuild(self, conn: asyncpg.Connection) -> None:
        await conn.execute("TRUNCATE TABLE agent_performance_ledger")
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position)
            VALUES ($1, 0)
            ON CONFLICT (projection_name) DO UPDATE SET last_position = 0, updated_at = NOW()
            """,
            self.name,
        )

    async def get_metrics(
        self, agent_id: str, model_version: str, conn: asyncpg.Connection
    ) -> dict | None:
        row = await conn.fetchrow(
            """
            SELECT
                agent_id, model_version,
                analyses_completed, decisions_generated,
                CASE WHEN analyses_completed > 0
                     THEN total_confidence_score / analyses_completed ELSE 0
                END AS avg_confidence_score,
                CASE WHEN analyses_completed > 0
                     THEN total_duration_ms / analyses_completed ELSE 0
                END AS avg_duration_ms,
                CASE WHEN decisions_generated > 0
                     THEN approve_count::float / decisions_generated ELSE 0
                END AS approve_rate,
                CASE WHEN decisions_generated > 0
                     THEN decline_count::float / decisions_generated ELSE 0
                END AS decline_rate,
                CASE WHEN decisions_generated > 0
                     THEN refer_count::float / decisions_generated ELSE 0
                END AS refer_rate,
                human_override_count,
                first_seen_at, last_seen_at
            FROM agent_performance_ledger
            WHERE agent_id = $1 AND model_version = $2
            """,
            agent_id, model_version,
        )
        return dict(row) if row else None

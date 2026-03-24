from __future__ import annotations

import asyncpg

from src.event_store import RecordedEvent
from src.projections.base import Projection


class ApplicationSummaryProjection(Projection):
    """
    Read-optimised view of every loan application's current state.

    One row per application_id. Updated by the daemon as events arrive.
    SLO: lag < 500ms in normal operation.

    Handles all LoanApplication lifecycle events. Idempotent: each handler
    uses INSERT ... ON CONFLICT DO UPDATE so replays are safe.
    """

    name = "ApplicationSummary"

    event_types = [
        "ApplicationSubmitted",
        "CreditAnalysisRequested",
        "CreditAnalysisCompleted",
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "DecisionGenerated",
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
        "FraudScreeningCompleted",
    ]

    async def handle(self, event: RecordedEvent, conn: asyncpg.Connection) -> None:
        p = event.payload
        app_id = p.get("application_id")
        if not app_id:
            return

        match event.event_type:
            case "ApplicationSubmitted":
                await conn.execute(
                    """
                    INSERT INTO application_summary
                        (application_id, state, applicant_id, requested_amount_usd,
                         last_event_type, last_event_at)
                    VALUES ($1, 'Submitted', $2, $3, $4, $5)
                    ON CONFLICT (application_id) DO UPDATE SET
                        state = 'Submitted',
                        applicant_id = EXCLUDED.applicant_id,
                        requested_amount_usd = EXCLUDED.requested_amount_usd,
                        last_event_type = EXCLUDED.last_event_type,
                        last_event_at = EXCLUDED.last_event_at,
                        updated_at = NOW()
                    """,
                    app_id,
                    p.get("applicant_id"),
                    p.get("requested_amount_usd"),
                    event.event_type,
                    event.recorded_at,
                )

            case "CreditAnalysisRequested":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = 'AwaitingAnalysis',
                        last_event_type = $2, last_event_at = $3, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id, event.event_type, event.recorded_at,
                )

            case "CreditAnalysisCompleted":
                session_id = f"agent-{p.get('agent_id', '')}-{p.get('session_id', '')}"
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = 'AnalysisComplete',
                        risk_tier = $2,
                        agent_sessions_completed = array_append(
                            agent_sessions_completed, $3
                        ),
                        last_event_type = $4, last_event_at = $5, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id,
                    p.get("risk_tier"),
                    session_id,
                    event.event_type,
                    event.recorded_at,
                )

            case "FraudScreeningCompleted":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET fraud_score = $2,
                        last_event_type = $3, last_event_at = $4, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id,
                    p.get("fraud_score"),
                    event.event_type,
                    event.recorded_at,
                )

            case "ComplianceCheckRequested":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = 'ComplianceReview',
                        compliance_status = 'in_progress',
                        last_event_type = $2, last_event_at = $3, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id, event.event_type, event.recorded_at,
                )

            case "ComplianceRulePassed":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET last_event_type = $2, last_event_at = $3, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id, event.event_type, event.recorded_at,
                )

            case "ComplianceRuleFailed":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET compliance_status = 'failed',
                        last_event_type = $2, last_event_at = $3, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id, event.event_type, event.recorded_at,
                )

            case "DecisionGenerated":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = 'PendingDecision',
                        decision = $2,
                        compliance_status = 'cleared',
                        last_event_type = $3, last_event_at = $4, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id,
                    p.get("recommendation"),
                    event.event_type,
                    event.recorded_at,
                )

            case "HumanReviewCompleted":
                final = p.get("final_decision", "")
                new_state = (
                    "ApprovedPendingHuman" if final == "APPROVE"
                    else "DeclinedPendingHuman"
                )
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = $2,
                        human_reviewer_id = $3,
                        last_event_type = $4, last_event_at = $5, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id,
                    new_state,
                    p.get("reviewer_id"),
                    event.event_type,
                    event.recorded_at,
                )

            case "ApplicationApproved":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = 'FinalApproved',
                        approved_amount_usd = $2,
                        final_decision_at = $3,
                        last_event_type = $4, last_event_at = $5, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id,
                    p.get("approved_amount_usd"),
                    event.recorded_at,
                    event.event_type,
                    event.recorded_at,
                )

            case "ApplicationDeclined":
                await conn.execute(
                    """
                    UPDATE application_summary
                    SET state = 'FinalDeclined',
                        final_decision_at = $2,
                        last_event_type = $3, last_event_at = $4, updated_at = NOW()
                    WHERE application_id = $1
                    """,
                    app_id,
                    event.recorded_at,
                    event.event_type,
                    event.recorded_at,
                )

    async def rebuild(self, conn: asyncpg.Connection) -> None:
        await conn.execute("TRUNCATE TABLE application_summary")
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position)
            VALUES ($1, 0)
            ON CONFLICT (projection_name) DO UPDATE SET last_position = 0, updated_at = NOW()
            """,
            self.name,
        )

    async def get_current(
        self, application_id: str, conn: asyncpg.Connection
    ) -> dict | None:
        row = await conn.fetchrow(
            "SELECT * FROM application_summary WHERE application_id = $1",
            application_id,
        )
        return dict(row) if row else None

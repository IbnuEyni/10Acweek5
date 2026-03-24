-- ============================================================
-- THE LEDGER — Agentic Event Store Schema
-- ============================================================
-- Every column is justified in DESIGN.md.
-- This schema is the contract all other components write to.
-- ============================================================

-- event_streams: one row per aggregate stream.
-- stream_id is TEXT (not UUID) to support human-readable IDs like "loan-{id}".
-- archived_at: NULL = active; non-NULL = soft-archived (data never deleted).
-- metadata: arbitrary stream-level tags (e.g. tenant_id, correlation context).
CREATE TABLE event_streams (
    stream_id        TEXT        PRIMARY KEY,          -- human-readable: "loan-{id}", "agent-{id}-{session}"
    aggregate_type   TEXT        NOT NULL,             -- discriminator for projection routing and aggregate rebuild
    current_version  BIGINT      NOT NULL DEFAULT 0,   -- monotonically increasing; used for optimistic concurrency check
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at      TIMESTAMPTZ,                      -- NULL = active; non-NULL = soft-archived; data never deleted
    metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb  -- tenant_id, correlation context, stream-level tags
);

-- events: the immutable append-only log. Nothing is ever updated or deleted here.
-- event_id: client-supplied UUID enables idempotent appends (retry safety).
-- stream_position: 1-based position within the stream; enforces ordering per stream.
-- global_position: DB-assigned total order across ALL streams for projection catch-up.
-- event_version: schema version of this event type; drives the upcaster chain.
-- payload: the domain fact — what happened.
-- metadata: cross-cutting concerns (correlation_id, causation_id, user_id, etc.).
-- recorded_at: clock_timestamp() (not now()) captures wall time at INSERT, not tx start.
-- UNIQUE (stream_id, stream_position): DB-level guard against duplicate appends.
CREATE TABLE events (
    event_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(), -- client-supplied for idempotent retries
    stream_id        TEXT        NOT NULL REFERENCES event_streams(stream_id) ON DELETE RESTRICT,
    stream_position  BIGINT      NOT NULL,             -- 1-based within stream; enforces per-stream ordering
    global_position  BIGINT      GENERATED ALWAYS AS IDENTITY, -- DB-assigned total order; never gaps, never reused
    event_type       TEXT        NOT NULL,             -- discriminator: "CreditAnalysisCompleted", etc.
    event_version    SMALLINT    NOT NULL DEFAULT 1,   -- schema version; drives the upcaster chain on read
    payload          JSONB       NOT NULL,             -- the domain fact; immutable after INSERT
    metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb, -- correlation_id, causation_id, user_id
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), -- wall time at INSERT, not transaction start

    -- Prevents duplicate appends at the same position — idempotency guard at DB level.
    -- Also the covering index for aggregate replay: filter stream_id, sort stream_position.
    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

-- Aggregate replay: WHERE stream_id = $x ORDER BY stream_position.
-- Composite covers both the equality filter and the sort — no separate sort step.
-- Hot path: every command handler calls load_stream() before appending.
CREATE INDEX idx_events_stream_id  ON events (stream_id, stream_position);

-- Projection catch-up: WHERE global_position > $last_checkpoint ORDER BY global_position.
-- ProjectionDaemon polls this in a tight loop; sequential scan of a narrow range.
CREATE INDEX idx_events_global_pos ON events (global_position);

-- Event-type filtering: load_all(event_types=[...]) pushes the filter to the DB.
-- Avoids transferring irrelevant events over the wire during projection rebuild.
CREATE INDEX idx_events_type       ON events (event_type);

-- Temporal range queries: regulatory examination, time-travel debugging, audit trail.
-- Supports get_compliance_at(application_id, timestamp) in ComplianceAuditView.
CREATE INDEX idx_events_recorded   ON events (recorded_at);

-- projection_checkpoints: one row per named projection.
-- last_position: the highest global_position this projection has successfully processed.
-- Daemon resumes from last_position + 1 after restart — no full replay needed.
CREATE TABLE projection_checkpoints (
    projection_name  TEXT        PRIMARY KEY,          -- e.g. "ApplicationSummary", "ComplianceAuditView"
    last_position    BIGINT      NOT NULL DEFAULT 0,   -- highest global_position successfully processed
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW() -- used to detect stale/stuck projections for SLO monitoring
    -- Daemon resumes from last_position + 1 after restart — no full replay needed.
    -- On first run last_position = 0 triggers a full rebuild from the beginning.
);

-- outbox: written atomically in the same transaction as the domain events.
-- Guarantees at-least-once delivery to downstream systems without two-phase commit.
-- destination: the target channel/topic (e.g. "kafka:loan-events", "redis:notifications").
-- published_at: NULL = pending; non-NULL = successfully relayed.
-- attempts: retry counter; relay worker backs off after N failures.
CREATE TABLE outbox (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID        NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    destination      TEXT        NOT NULL,             -- target channel: "kafka:loan-events", "redis:notifications"
    payload          JSONB       NOT NULL,             -- denormalised snapshot; relay worker needs no JOIN
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at     TIMESTAMPTZ,                      -- NULL = pending relay; non-NULL = successfully delivered
    attempts         SMALLINT    NOT NULL DEFAULT 0    -- relay worker increments; backs off after N failures
    -- Written atomically in the same transaction as domain events.
    -- If the events INSERT fails this row is also rolled back — guaranteed at-least-once
    -- delivery without two-phase commit. Only the relay worker writes published_at.
);

-- Partial index: relay worker polls only unpublished rows (WHERE published_at IS NULL).
-- Published rows excluded entirely — index stays small as the outbox drains.
-- Without this a full table scan would grow unboundedly as history accumulates.
CREATE INDEX idx_outbox_pending ON outbox (created_at) WHERE published_at IS NULL;

-- ============================================================
-- PHASE 3 — Projection Read Models
-- ============================================================

-- application_summary: one row per loan application, current state.
-- SLO: lag < 500ms. Updated by ProjectionDaemon as events arrive.
-- agent_sessions_completed: array of session stream IDs that contributed decisions.
CREATE TABLE application_summary (
    application_id          TEXT        PRIMARY KEY,
    state                   TEXT        NOT NULL DEFAULT 'Submitted',
    applicant_id            TEXT,
    requested_amount_usd    NUMERIC,
    approved_amount_usd     NUMERIC,
    risk_tier               TEXT,
    fraud_score             NUMERIC,
    compliance_status       TEXT        NOT NULL DEFAULT 'pending',  -- pending|in_progress|cleared|failed
    decision                TEXT,                                    -- APPROVE|DECLINE|REFER|NULL
    agent_sessions_completed TEXT[]     NOT NULL DEFAULT '{}',
    last_event_type         TEXT,
    last_event_at           TIMESTAMPTZ,
    human_reviewer_id       TEXT,
    final_decision_at       TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- agent_performance_ledger: aggregated metrics per agent+model_version.
-- SLO: lag < 500ms. Enables systematic model drift detection.
CREATE TABLE agent_performance_ledger (
    agent_id                TEXT        NOT NULL,
    model_version           TEXT        NOT NULL,
    analyses_completed      INT         NOT NULL DEFAULT 0,
    decisions_generated     INT         NOT NULL DEFAULT 0,
    total_confidence_score  NUMERIC     NOT NULL DEFAULT 0,  -- sum; divide by analyses_completed for avg
    total_duration_ms       BIGINT      NOT NULL DEFAULT 0,  -- sum; divide by analyses_completed for avg
    approve_count           INT         NOT NULL DEFAULT 0,
    decline_count           INT         NOT NULL DEFAULT 0,
    refer_count             INT         NOT NULL DEFAULT 0,
    human_override_count    INT         NOT NULL DEFAULT 0,
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, model_version)
);

-- compliance_audit_snapshots: point-in-time snapshots for temporal queries.
-- Snapshot strategy: event-count trigger (every 10 compliance events per application).
-- Enables get_compliance_at(application_id, timestamp) without full replay.
-- snapshot_at: the recorded_at of the last event included in this snapshot.
CREATE TABLE compliance_audit_snapshots (
    id                      BIGSERIAL   PRIMARY KEY,
    application_id          TEXT        NOT NULL,
    snapshot_at             TIMESTAMPTZ NOT NULL,  -- wall time of last included event
    last_global_position    BIGINT      NOT NULL,  -- global_position of last included event
    state                   JSONB       NOT NULL,  -- full serialised ComplianceState
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_compliance_snapshots_app_time
    ON compliance_audit_snapshots (application_id, snapshot_at DESC);

-- compliance_audit_events: append-only log of every compliance event per application.
-- Supports full audit trail and temporal replay from any snapshot.
CREATE TABLE compliance_audit_events (
    id                      BIGSERIAL   PRIMARY KEY,
    application_id          TEXT        NOT NULL,
    global_position         BIGINT      NOT NULL,
    event_type              TEXT        NOT NULL,
    rule_id                 TEXT,
    rule_version            TEXT,
    regulation_set_version  TEXT,
    result                  TEXT,        -- passed|failed|requested|cleared
    failure_reason          TEXT,
    evidence_hash           TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_compliance_audit_events_app
    ON compliance_audit_events (application_id, recorded_at);

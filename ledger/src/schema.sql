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
    stream_id        TEXT        PRIMARY KEY,
    aggregate_type   TEXT        NOT NULL,
    current_version  BIGINT      NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at      TIMESTAMPTZ,
    metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb
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
    event_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id        TEXT        NOT NULL REFERENCES event_streams(stream_id),
    stream_position  BIGINT      NOT NULL,
    global_position  BIGINT      GENERATED ALWAYS AS IDENTITY,
    event_type       TEXT        NOT NULL,
    event_version    SMALLINT    NOT NULL DEFAULT 1,
    payload          JSONB       NOT NULL,
    metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

-- Aggregate replay: WHERE stream_id = $x ORDER BY stream_position.
-- Composite covers both the filter and the sort — no separate sort step.
CREATE INDEX idx_events_stream_id  ON events (stream_id, stream_position);
-- Projection catch-up: WHERE global_position > $last_checkpoint ORDER BY global_position.
CREATE INDEX idx_events_global_pos ON events (global_position);
-- Event-type filtering: ProjectionDaemon routes by event_type; load_all() filters by type.
CREATE INDEX idx_events_type       ON events (event_type);
-- Temporal range queries: auditing, time-travel debugging, regulatory examination.
CREATE INDEX idx_events_recorded   ON events (recorded_at);

-- projection_checkpoints: one row per named projection.
-- last_position: the highest global_position this projection has successfully processed.
-- Daemon resumes from last_position + 1 after restart — no full replay needed.
CREATE TABLE projection_checkpoints (
    projection_name  TEXT        PRIMARY KEY,
    last_position    BIGINT      NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- outbox: written atomically in the same transaction as the domain events.
-- Guarantees at-least-once delivery to downstream systems without two-phase commit.
-- destination: the target channel/topic (e.g. "kafka:loan-events", "redis:notifications").
-- published_at: NULL = pending; non-NULL = successfully relayed.
-- attempts: retry counter; relay worker backs off after N failures.
CREATE TABLE outbox (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         UUID        NOT NULL REFERENCES events(event_id),
    destination      TEXT        NOT NULL,
    payload          JSONB       NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at     TIMESTAMPTZ,
    attempts         SMALLINT    NOT NULL DEFAULT 0
);

-- Partial index: relay worker polls only unpublished rows.
-- Published rows are excluded entirely — index stays small as the outbox drains.
CREATE INDEX idx_outbox_pending ON outbox (created_at) WHERE published_at IS NULL;

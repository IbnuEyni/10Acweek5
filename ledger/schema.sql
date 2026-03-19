-- ============================================================
-- THE LEDGER — Agentic Event Store Schema
-- ============================================================

-- Tracks aggregate streams and their current version.
-- current_version enables optimistic concurrency control:
-- writers assert the version they expect before appending.
CREATE TABLE event_streams (
    stream_id       UUID        PRIMARY KEY,
    aggregate_type  TEXT        NOT NULL,
    current_version BIGINT      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Core append-only event log.
-- global_position: monotonically increasing identity column used by
--   projections and subscribers to consume events in total order.
-- stream_position: per-stream sequence number for optimistic locking.
-- UNIQUE (stream_id, stream_position): prevents duplicate writes at
--   the same position within a stream (idempotency guard).
CREATE TABLE events (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id       UUID        NOT NULL REFERENCES event_streams(stream_id),
    stream_position BIGINT      NOT NULL,
    global_position BIGINT      GENERATED ALWAYS AS IDENTITY,
    event_type      TEXT        NOT NULL,
    payload         JSONB       NOT NULL DEFAULT '{}',
    metadata        JSONB       NOT NULL DEFAULT '{}',
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

-- Index: global_position — primary read path for all projections and
--   catch-up subscribers scanning the log in total order.
CREATE INDEX idx_events_global_position ON events (global_position);

-- Index: (stream_id, stream_position) — point lookups and range scans
--   when replaying a single aggregate's history.
CREATE INDEX idx_events_stream ON events (stream_id, stream_position);

-- Index: occurred_at — temporal range queries (e.g. "events in last 5 min")
--   used by monitoring, auditing, and time-travel debugging.
CREATE INDEX idx_events_occurred_at ON events (occurred_at);

-- Tracks the last global_position processed by each async read model.
-- Enables safe resume after restart without reprocessing the full log.
CREATE TABLE projection_checkpoints (
    projection_name  TEXT        PRIMARY KEY,
    last_position    BIGINT      NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Outbox table for guaranteed at-least-once delivery to external systems.
-- Rows are written in the same DB transaction as the domain event,
-- then polled and published by a relay process.
-- processed_at NULL means pending; set on successful publish.
CREATE TABLE outbox (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id       UUID        NOT NULL,
    event_id        UUID        NOT NULL REFERENCES events(id),
    event_type      TEXT        NOT NULL,
    payload         JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

-- Index: (processed_at, created_at) — the relay polls WHERE processed_at IS NULL
--   ORDER BY created_at to deliver in insertion order with minimal scan cost.
CREATE INDEX idx_outbox_pending ON outbox (created_at)
    WHERE processed_at IS NULL;

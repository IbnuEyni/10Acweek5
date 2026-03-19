-- ============================================================
-- THE LEDGER — Agentic Event Store Schema
-- ============================================================

CREATE TABLE event_streams (
    stream_id       UUID        PRIMARY KEY,
    aggregate_type  TEXT        NOT NULL,
    current_version BIGINT      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- global_position: IDENTITY column — DB-assigned total order for all projections.
-- UNIQUE (stream_id, stream_position): DB-level idempotency guard against duplicate appends.
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

-- Projection catch-up: every subscriber scans WHERE global_position > $last in total order.
CREATE INDEX idx_events_global_position ON events (global_position);
-- Aggregate replay: WHERE stream_id = $x ORDER BY stream_position — composite covers filter + sort.
CREATE INDEX idx_events_stream ON events (stream_id, stream_position);
-- Temporal range queries for auditing / time-travel debugging.
CREATE INDEX idx_events_occurred_at ON events (occurred_at);

-- Tracks the last global_position consumed by each async read model.
CREATE TABLE projection_checkpoints (
    projection_name TEXT        PRIMARY KEY,
    last_position   BIGINT      NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Outbox: written in the same transaction as the domain event.
-- processed_at IS NULL  →  pending delivery.
CREATE TABLE outbox (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id    UUID        NOT NULL,
    event_id     UUID        NOT NULL REFERENCES events(id),
    event_type   TEXT        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- Partial index: relay polls only unprocessed rows; processed rows are excluded entirely.
CREATE INDEX idx_outbox_pending ON outbox (created_at) WHERE processed_at IS NULL;

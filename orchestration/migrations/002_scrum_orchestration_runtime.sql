CREATE TABLE IF NOT EXISTS kanban_projections (
    projection_id bigserial PRIMARY KEY,
    scope_type text NOT NULL,
    scope_id text NOT NULL,
    board_name text NOT NULL,
    projection_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_event_id text REFERENCES workflow_events(event_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_type, scope_id, board_name)
);

CREATE INDEX IF NOT EXISTS kanban_projections_scope_idx
    ON kanban_projections(scope_type, scope_id);

CREATE INDEX IF NOT EXISTS work_orders_timeout_scan_idx
    ON work_orders(status, updated_at, timeout_seconds)
    WHERE status IN ('pending', 'dispatched', 'running');

CREATE INDEX IF NOT EXISTS work_orders_metadata_sprint_idx
    ON work_orders ((metadata->>'sprint_id'))
    WHERE metadata ? 'sprint_id';

CREATE INDEX IF NOT EXISTS step_runs_metadata_sprint_idx
    ON step_runs ((metadata->>'sprint_id'))
    WHERE metadata ? 'sprint_id';


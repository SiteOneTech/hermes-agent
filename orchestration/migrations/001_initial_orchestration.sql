CREATE TABLE IF NOT EXISTS workflow_definitions (
    workflow_definition_id text PRIMARY KEY,
    domain text NOT NULL,
    display_name text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    workflow_definition_id text NOT NULL REFERENCES workflow_definitions(workflow_definition_id),
    workflow_version text NOT NULL,
    definition_json jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    PRIMARY KEY (workflow_definition_id, workflow_version)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_run_id text PRIMARY KEY,
    workflow_definition_id text NOT NULL,
    workflow_version text NOT NULL,
    title text NOT NULL,
    status text NOT NULL,
    current_step_id text,
    created_by text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS step_runs (
    step_run_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    step_key text NOT NULL,
    owner_role text NOT NULL,
    status text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'workflow_runs_current_step_fk'
    ) THEN
        ALTER TABLE workflow_runs
            ADD CONSTRAINT workflow_runs_current_step_fk
            FOREIGN KEY (current_step_id) REFERENCES step_runs(step_run_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    step_run_id text NOT NULL REFERENCES step_runs(step_run_id) ON DELETE CASCADE,
    owner_role text NOT NULL,
    task text NOT NULL,
    status text NOT NULL,
    required_outputs jsonb NOT NULL DEFAULT '[]'::jsonb,
    inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    timeout_seconds integer NOT NULL DEFAULT 1800,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS work_order_attempts (
    attempt_id text PRIMARY KEY,
    work_order_id text NOT NULL REFERENCES work_orders(work_order_id) ON DELETE CASCADE,
    worker_node_id text,
    adapter text NOT NULL,
    status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    last_heartbeat_at timestamptz,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_category text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflow_events (
    event_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    step_run_id text REFERENCES step_runs(step_run_id) ON DELETE SET NULL,
    work_order_id text REFERENCES work_orders(work_order_id) ON DELETE SET NULL,
    event_type text NOT NULL,
    actor text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS workflow_events_idempotency_key_uq
    ON workflow_events(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS gate_reviews (
    gate_review_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    step_run_id text NOT NULL REFERENCES step_runs(step_run_id) ON DELETE CASCADE,
    requested_by text NOT NULL,
    reviewer_role text NOT NULL,
    status text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    notes text NOT NULL DEFAULT '',
    decision_by text,
    decision_notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS human_decisions (
    human_decision_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    gate_review_id text REFERENCES gate_reviews(gate_review_id) ON DELETE SET NULL,
    actor text NOT NULL,
    decision text NOT NULL,
    reason text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artifact_refs (
    artifact_ref_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    step_run_id text REFERENCES step_runs(step_run_id) ON DELETE SET NULL,
    work_order_id text REFERENCES work_orders(work_order_id) ON DELETE SET NULL,
    artifact_type text NOT NULL,
    uri text NOT NULL,
    sha256 text,
    produced_by text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_observability_logs (
    log_id text PRIMARY KEY,
    workflow_run_id text NOT NULL REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    step_run_id text REFERENCES step_runs(step_run_id) ON DELETE SET NULL,
    work_order_id text REFERENCES work_orders(work_order_id) ON DELETE SET NULL,
    agent_id text NOT NULL,
    iteration integer NOT NULL DEFAULT 1,
    uri text NOT NULL,
    sha256 text,
    summary text NOT NULL DEFAULT '',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_nodes (
    worker_node_id text PRIMARY KEY,
    branch_id text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL,
    endpoint text,
    last_heartbeat_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_capabilities (
    worker_node_id text NOT NULL REFERENCES worker_nodes(worker_node_id) ON DELETE CASCADE,
    capability text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (worker_node_id, capability)
);

CREATE TABLE IF NOT EXISTS inbox_messages (
    inbox_message_id bigserial PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'received',
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);

CREATE TABLE IF NOT EXISTS outbox_messages (
    outbox_message_id bigserial PRIMARY KEY,
    workflow_run_id text REFERENCES workflow_runs(workflow_run_id) ON DELETE CASCADE,
    target text NOT NULL,
    message_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);

CREATE TABLE IF NOT EXISTS dead_letters (
    dead_letter_id bigserial PRIMARY KEY,
    source_table text NOT NULL,
    source_id text NOT NULL,
    reason text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS projection_offsets (
    projection_name text PRIMARY KEY,
    last_event_created_at timestamptz,
    last_event_id text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS workflow_runs_status_idx ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS step_runs_workflow_status_idx ON step_runs(workflow_run_id, status);
CREATE INDEX IF NOT EXISTS work_orders_workflow_status_idx ON work_orders(workflow_run_id, status);
CREATE INDEX IF NOT EXISTS workflow_events_run_created_idx ON workflow_events(workflow_run_id, created_at);
CREATE INDEX IF NOT EXISTS workflow_events_type_created_idx ON workflow_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS gate_reviews_workflow_status_idx ON gate_reviews(workflow_run_id, status);
CREATE INDEX IF NOT EXISTS artifact_refs_workflow_type_idx ON artifact_refs(workflow_run_id, artifact_type);
CREATE INDEX IF NOT EXISTS agent_logs_workflow_agent_idx ON agent_observability_logs(workflow_run_id, agent_id);
CREATE INDEX IF NOT EXISTS outbox_status_available_idx ON outbox_messages(status, available_at);

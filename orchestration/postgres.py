"""PostgreSQL repository and migration support for orchestration."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestration.domain import (
    AgentObservabilityLog,
    ArtifactRef,
    GateReview,
    GateStatus,
    StepRun,
    StepStatus,
    WorkflowEvent,
    WorkflowRun,
    WorkflowRunStatus,
    WorkOrder,
    WorkOrderStatus,
)
from orchestration.repository import EntityNotFound

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - exercised in deployments without extra
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]


MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def _require_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError(
            "PostgreSQL support requires psycopg. Install with "
            "'pip install \"hermes-agent[orchestration]\"'."
        )


def _json(value: Any) -> Any:
    _require_psycopg()
    return Jsonb(value if value is not None else {})


def _list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return list(value)


def _dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return dict(value)


class PostgresMigrationRunner:
    def __init__(self, database_url: str, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        self._database_url = database_url
        self._migrations_dir = migrations_dir

    def apply(self) -> list[str]:
        _require_psycopg()
        applied: list[str] = []
        with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            seen = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
            for path in sorted(self._migrations_dir.glob("*.sql")):
                version = path.name
                if version in seen:
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                applied.append(version)
            conn.commit()
        return applied


class PostgresOrchestrationRepository:
    """PostgreSQL-backed repository for the orchestration control plane."""

    def __init__(self, database_url: str) -> None:
        _require_psycopg()
        self._database_url = database_url

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
            yield conn

    def upsert_workflow_definition(
        self,
        *,
        workflow_definition_id: str,
        domain: str,
        display_name: str,
        description: str,
        workflow_version: str,
        definition_json: dict,
        status: str = "published",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    workflow_definition_id, domain, display_name, description
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (workflow_definition_id) DO UPDATE SET
                    domain = excluded.domain,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    updated_at = now()
                """,
                (workflow_definition_id, domain, display_name, description),
            )
            conn.execute(
                """
                INSERT INTO workflow_versions (
                    workflow_definition_id, workflow_version, definition_json,
                    status, published_at
                ) VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (workflow_definition_id, workflow_version) DO UPDATE SET
                    definition_json = excluded.definition_json,
                    status = excluded.status,
                    published_at = COALESCE(workflow_versions.published_at, now())
                """,
                (
                    workflow_definition_id,
                    workflow_version,
                    _json(definition_json),
                    status,
                ),
            )
            conn.commit()

    def list_workflow_definitions(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.workflow_definition_id, d.domain, d.display_name,
                       d.description, v.workflow_version, v.definition_json,
                       v.status, v.published_at
                FROM workflow_definitions d
                JOIN workflow_versions v
                  ON v.workflow_definition_id = d.workflow_definition_id
                ORDER BY d.workflow_definition_id, v.workflow_version
                """
            ).fetchall()
        return [
            {
                "workflow_definition_id": row["workflow_definition_id"],
                "domain": row["domain"],
                "display_name": row["display_name"],
                "description": row["description"],
                "workflow_version": row["workflow_version"],
                "definition_json": _dict(row["definition_json"]),
                "status": row["status"],
                "published_at": None
                if row["published_at"] is None
                else row["published_at"].isoformat(),
            }
            for row in rows
        ]

    def create_workflow_run(self, run: WorkflowRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    workflow_run_id, workflow_definition_id, workflow_version,
                    title, status, current_step_id, created_by, metadata,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.workflow_run_id,
                    run.workflow_definition_id,
                    run.workflow_version,
                    run.title,
                    run.status.value,
                    run.current_step_id,
                    run.created_by,
                    _json(run.metadata),
                    run.created_at,
                    run.updated_at,
                ),
            )
            conn.commit()

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_run_id = %s",
                (workflow_run_id,),
            ).fetchone()
        if not row:
            raise EntityNotFound(workflow_run_id)
        return WorkflowRun(
            workflow_run_id=row["workflow_run_id"],
            workflow_definition_id=row["workflow_definition_id"],
            workflow_version=row["workflow_version"],
            title=row["title"],
            status=WorkflowRunStatus(row["status"]),
            current_step_id=row["current_step_id"],
            created_by=row["created_by"],
            metadata=_dict(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_workflow_run(self, run: WorkflowRun) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE workflow_runs
                SET title = %s, status = %s, current_step_id = %s,
                    metadata = %s, updated_at = %s
                WHERE workflow_run_id = %s
                """,
                (
                    run.title,
                    run.status.value,
                    run.current_step_id,
                    _json(run.metadata),
                    run.updated_at,
                    run.workflow_run_id,
                ),
            )
            if cur.rowcount != 1:
                raise EntityNotFound(run.workflow_run_id)
            conn.commit()

    def create_step_run(self, step: StepRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO step_runs (
                    step_run_id, workflow_run_id, step_key, owner_role, status,
                    metadata, started_at, completed_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    step.step_run_id,
                    step.workflow_run_id,
                    step.step_key,
                    step.owner_role,
                    step.status.value,
                    _json(step.metadata),
                    step.started_at,
                    step.completed_at,
                    step.created_at,
                    step.updated_at,
                ),
            )
            conn.commit()

    def get_step_run(self, step_run_id: str) -> StepRun:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM step_runs WHERE step_run_id = %s",
                (step_run_id,),
            ).fetchone()
        if not row:
            raise EntityNotFound(step_run_id)
        return StepRun(
            step_run_id=row["step_run_id"],
            workflow_run_id=row["workflow_run_id"],
            step_key=row["step_key"],
            owner_role=row["owner_role"],
            status=StepStatus(row["status"]),
            metadata=_dict(row["metadata"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_step_runs(self, workflow_run_id: str) -> list[StepRun]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM step_runs
                WHERE workflow_run_id = %s
                ORDER BY created_at, step_run_id
                """,
                (workflow_run_id,),
            ).fetchall()
        return [
            StepRun(
                step_run_id=row["step_run_id"],
                workflow_run_id=row["workflow_run_id"],
                step_key=row["step_key"],
                owner_role=row["owner_role"],
                status=StepStatus(row["status"]),
                metadata=_dict(row["metadata"]),
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def update_step_run(self, step: StepRun) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE step_runs
                SET owner_role = %s, status = %s, metadata = %s,
                    started_at = %s, completed_at = %s, updated_at = %s
                WHERE step_run_id = %s
                """,
                (
                    step.owner_role,
                    step.status.value,
                    _json(step.metadata),
                    step.started_at,
                    step.completed_at,
                    step.updated_at,
                    step.step_run_id,
                ),
            )
            if cur.rowcount != 1:
                raise EntityNotFound(step.step_run_id)
            conn.commit()

    def create_work_order(self, work_order: WorkOrder) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO work_orders (
                    work_order_id, workflow_run_id, step_run_id, owner_role,
                    task, status, required_outputs, inputs, timeout_seconds,
                    metadata, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    work_order.work_order_id,
                    work_order.workflow_run_id,
                    work_order.step_run_id,
                    work_order.owner_role,
                    work_order.task,
                    work_order.status.value,
                    _json(work_order.required_outputs),
                    _json(work_order.inputs),
                    work_order.timeout_seconds,
                    _json(work_order.metadata),
                    work_order.created_at,
                    work_order.updated_at,
                ),
            )
            conn.commit()

    def get_work_order(self, work_order_id: str) -> WorkOrder:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_orders WHERE work_order_id = %s",
                (work_order_id,),
            ).fetchone()
        if not row:
            raise EntityNotFound(work_order_id)
        return WorkOrder(
            work_order_id=row["work_order_id"],
            workflow_run_id=row["workflow_run_id"],
            step_run_id=row["step_run_id"],
            owner_role=row["owner_role"],
            task=row["task"],
            status=WorkOrderStatus(row["status"]),
            required_outputs=_list(row["required_outputs"]),
            inputs=_dict(row["inputs"]),
            timeout_seconds=row["timeout_seconds"],
            metadata=_dict(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_work_orders(self, workflow_run_id: str) -> list[WorkOrder]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM work_orders
                WHERE workflow_run_id = %s
                ORDER BY created_at, work_order_id
                """,
                (workflow_run_id,),
            ).fetchall()
        return [
            WorkOrder(
                work_order_id=row["work_order_id"],
                workflow_run_id=row["workflow_run_id"],
                step_run_id=row["step_run_id"],
                owner_role=row["owner_role"],
                task=row["task"],
                status=WorkOrderStatus(row["status"]),
                required_outputs=_list(row["required_outputs"]),
                inputs=_dict(row["inputs"]),
                timeout_seconds=row["timeout_seconds"],
                metadata=_dict(row["metadata"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def list_stale_work_orders(self, now: datetime) -> list[WorkOrder]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM work_orders
                WHERE status IN ('pending', 'dispatched', 'running')
                  AND updated_at + (timeout_seconds * interval '1 second') <= %s
                ORDER BY updated_at, work_order_id
                """,
                (now,),
            ).fetchall()
        return [
            WorkOrder(
                work_order_id=row["work_order_id"],
                workflow_run_id=row["workflow_run_id"],
                step_run_id=row["step_run_id"],
                owner_role=row["owner_role"],
                task=row["task"],
                status=WorkOrderStatus(row["status"]),
                required_outputs=_list(row["required_outputs"]),
                inputs=_dict(row["inputs"]),
                timeout_seconds=row["timeout_seconds"],
                metadata=_dict(row["metadata"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def update_work_order(self, work_order: WorkOrder) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE work_orders
                SET owner_role = %s, task = %s, status = %s,
                    required_outputs = %s, inputs = %s, timeout_seconds = %s,
                    metadata = %s, updated_at = %s
                WHERE work_order_id = %s
                """,
                (
                    work_order.owner_role,
                    work_order.task,
                    work_order.status.value,
                    _json(work_order.required_outputs),
                    _json(work_order.inputs),
                    work_order.timeout_seconds,
                    _json(work_order.metadata),
                    work_order.updated_at,
                    work_order.work_order_id,
                ),
            )
            if cur.rowcount != 1:
                raise EntityNotFound(work_order.work_order_id)
            conn.commit()

    def save_gate_review(self, gate: GateReview) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gate_reviews (
                    gate_review_id, workflow_run_id, step_run_id, requested_by,
                    reviewer_role, status, evidence_refs, notes, decision_by,
                    decision_notes, created_at, decided_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    gate.gate_review_id,
                    gate.workflow_run_id,
                    gate.step_run_id,
                    gate.requested_by,
                    gate.reviewer_role,
                    gate.status.value,
                    _json(gate.evidence_refs),
                    gate.notes,
                    gate.decision_by,
                    gate.decision_notes,
                    gate.created_at,
                    gate.decided_at,
                    gate.updated_at,
                ),
            )
            conn.commit()

    def get_gate_review(self, gate_review_id: str) -> GateReview:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gate_reviews WHERE gate_review_id = %s",
                (gate_review_id,),
            ).fetchone()
        if not row:
            raise EntityNotFound(gate_review_id)
        return GateReview(
            gate_review_id=row["gate_review_id"],
            workflow_run_id=row["workflow_run_id"],
            step_run_id=row["step_run_id"],
            requested_by=row["requested_by"],
            reviewer_role=row["reviewer_role"],
            status=GateStatus(row["status"]),
            evidence_refs=_list(row["evidence_refs"]),
            notes=row["notes"] or "",
            decision_by=row["decision_by"],
            decision_notes=row["decision_notes"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            updated_at=row["updated_at"],
        )

    def update_gate_review(self, gate: GateReview) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE gate_reviews
                SET status = %s, evidence_refs = %s, notes = %s,
                    decision_by = %s, decision_notes = %s,
                    decided_at = %s, updated_at = %s
                WHERE gate_review_id = %s
                """,
                (
                    gate.status.value,
                    _json(gate.evidence_refs),
                    gate.notes,
                    gate.decision_by,
                    gate.decision_notes,
                    gate.decided_at,
                    gate.updated_at,
                    gate.gate_review_id,
                ),
            )
            if cur.rowcount != 1:
                raise EntityNotFound(gate.gate_review_id)
            conn.commit()

    def append_event(self, event: WorkflowEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_events (
                    event_id, workflow_run_id, step_run_id, work_order_id,
                    event_type, actor, payload, idempotency_key, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
                DO NOTHING
                """,
                (
                    event.event_id,
                    event.workflow_run_id,
                    event.step_run_id,
                    event.work_order_id,
                    event.event_type,
                    event.actor,
                    _json(event.payload),
                    event.idempotency_key,
                    event.created_at,
                ),
            )
            conn.commit()

    def list_events(self, workflow_run_id: str) -> list[WorkflowEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_events
                WHERE workflow_run_id = %s
                ORDER BY created_at, event_id
                """,
                (workflow_run_id,),
            ).fetchall()
        return [
            WorkflowEvent(
                event_id=row["event_id"],
                workflow_run_id=row["workflow_run_id"],
                step_run_id=row["step_run_id"],
                work_order_id=row["work_order_id"],
                event_type=row["event_type"],
                actor=row["actor"],
                payload=_dict(row["payload"]),
                idempotency_key=row["idempotency_key"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_artifact_ref(self, artifact: ArtifactRef) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifact_refs (
                    artifact_ref_id, workflow_run_id, step_run_id, work_order_id,
                    artifact_type, uri, sha256, produced_by, metadata, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_ref_id) DO NOTHING
                """,
                (
                    artifact.artifact_ref_id,
                    artifact.workflow_run_id,
                    artifact.step_run_id,
                    artifact.work_order_id,
                    artifact.artifact_type,
                    artifact.uri,
                    artifact.sha256,
                    artifact.produced_by,
                    _json(artifact.metadata),
                    artifact.created_at,
                ),
            )
            conn.commit()

    def add_agent_log(self, log: AgentObservabilityLog) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_observability_logs (
                    log_id, workflow_run_id, step_run_id, work_order_id,
                    agent_id, iteration, uri, sha256, summary, metadata, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (log_id) DO NOTHING
                """,
                (
                    log.log_id,
                    log.workflow_run_id,
                    log.step_run_id,
                    log.work_order_id,
                    log.agent_id,
                    log.iteration,
                    log.uri,
                    log.sha256,
                    log.summary,
                    _json(log.metadata),
                    log.created_at,
                ),
            )
            conn.commit()

    def record_inbox_message(self, idempotency_key: str, payload: dict) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO inbox_messages (idempotency_key, payload, status)
                VALUES (%s, %s, 'processed')
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (idempotency_key, _json(payload)),
            )
            conn.commit()
        return cur.rowcount == 1

    def upsert_kanban_projection(
        self,
        *,
        scope_type: str,
        scope_id: str,
        board_name: str,
        projection_json: dict,
        source_event_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kanban_projections (
                    scope_type, scope_id, board_name, projection_json,
                    source_event_id, updated_at
                ) VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (scope_type, scope_id, board_name) DO UPDATE SET
                    projection_json = excluded.projection_json,
                    source_event_id = excluded.source_event_id,
                    updated_at = now()
                """,
                (
                    scope_type,
                    scope_id,
                    board_name,
                    _json(projection_json),
                    source_event_id,
                ),
            )
            conn.commit()

    def get_kanban_projection(
        self,
        *,
        scope_type: str,
        scope_id: str,
        board_name: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT projection_json FROM kanban_projections
                WHERE scope_type = %s AND scope_id = %s AND board_name = %s
                """,
                (scope_type, scope_id, board_name),
            ).fetchone()
        if not row:
            return None
        return _dict(row["projection_json"])


def dumps_safe(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)

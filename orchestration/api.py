"""FastAPI routes for Hermes orchestration."""

from __future__ import annotations

import hmac
import os
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field

try:
    from fastapi import APIRouter, Depends, Header, HTTPException, Request
except ImportError:  # pragma: no cover
    APIRouter = None  # type: ignore[assignment]
    Depends = None  # type: ignore[assignment]
    Header = None  # type: ignore[assignment]
    HTTPException = Exception  # type: ignore[assignment]
    Request = object  # type: ignore[assignment]

from orchestration.domain import (
    AgentObservabilityLog,
    ArtifactRef,
    GateDecision,
    WorkOrderCallback,
    WorkOrderStatus,
    new_id,
)
from orchestration.postgres import PostgresOrchestrationRepository
from orchestration.service import OrchestrationError, OrchestrationService


class CreateWorkflowRunRequest(BaseModel):
    workflow_definition_id: str
    workflow_version: str = "1.0.0"
    title: str
    description: str = ""
    created_by: str
    initial_step_key: str = "INTAKE"
    initial_owner_role: str = "zeus"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateWorkOrderRequest(BaseModel):
    step_run_id: str
    owner_role: str
    task: str
    required_outputs: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 1800
    actor: str = "leo-orquestador"


class ArtifactRefPayload(BaseModel):
    artifact_type: str
    uri: str
    produced_by: str
    artifact_ref_id: str | None = None
    step_run_id: str | None = None
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentLogPayload(BaseModel):
    agent_id: str
    uri: str
    log_id: str | None = None
    step_run_id: str | None = None
    iteration: int = 1
    summary: str = ""
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkOrderCallbackRequest(BaseModel):
    attempt_id: str
    status: WorkOrderStatus
    actor: str
    commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRefPayload] = Field(default_factory=list)
    agent_logs: list[AgentLogPayload] = Field(default_factory=list)
    test_results: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class GateDecisionRequest(BaseModel):
    decision: GateDecision
    reviewer_role: str
    notes: str = ""


class FactoryEventRequest(BaseModel):
    workflow_run_id: str
    event_type: str
    actor: str
    payload: dict[str, Any] = Field(default_factory=dict)
    step_run_id: str | None = None
    work_order_id: str | None = None
    idempotency_key: str


class SprintBacklogItem(BaseModel):
    task: str
    owner_role: str
    required_outputs: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 1800
    backlog_item_id: str | None = None
    work_order_id: str | None = None
    expected_first_heartbeat_seconds: int | None = None
    retry_policy: dict[str, Any] = Field(default_factory=dict)


class FactoryScrumProjectRequest(BaseModel):
    title: str
    objective: str
    created_by: str = "zeus"
    project_id: str
    branch_id: str = "sicilia"
    complexity: str = "standard"
    autonomy_level: str = "L2"
    sprint_goal: str = ""
    backlog_items: list[SprintBacklogItem] = Field(default_factory=list)


class OpenSprintRequest(BaseModel):
    sprint_id: str
    sprint_goal: str
    actor: str = "ana-pmo"
    backlog_items: list[SprintBacklogItem]


class CloseSprintRequest(BaseModel):
    actor: str = "ana-pmo"
    review_notes: str
    retrospective_notes: str
    force: bool = False


class HeartbeatRequest(BaseModel):
    actor: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class GateReviewRequest(BaseModel):
    step_run_id: str
    requested_by: str
    reviewer_role: str = "zeus"
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str = ""


class InterventionRequest(BaseModel):
    actor: str = "zeus"
    reason: str
    action: str = "inspect"
    notes: str = ""
    work_order_id: str | None = None


class ResolveInterventionRequest(BaseModel):
    actor: str = "zeus"
    reason: str
    outcome: str = "resolved"
    notes: str = ""
    work_order_id: str | None = None


class WatchdogRunRequest(BaseModel):
    actor: str = "zeus-watchdog"


def _require_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("HERMES_ORCHESTRATION_API_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Hermes orchestration API key is not configured.",
        )
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = authorization[len(prefix):]
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@lru_cache(maxsize=1)
def _service() -> OrchestrationService:
    database_url = os.getenv("HERMES_ORCHESTRATION_DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("HERMES_ORCHESTRATION_DATABASE_URL is not configured")
    return OrchestrationService(PostgresOrchestrationRepository(database_url))


def _get_service() -> OrchestrationService:
    try:
        return _service()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _workflow_run_payload(run: Any) -> dict[str, Any]:
    return {
        "workflow_run_id": run.workflow_run_id,
        "workflow_definition_id": run.workflow_definition_id,
        "workflow_version": run.workflow_version,
        "title": run.title,
        "description": run.description,
        "status": run.status.value,
        "current_step_id": run.current_step_id,
        "created_by": run.created_by,
        "metadata": run.metadata,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _step_payload(step: Any) -> dict[str, Any]:
    return {
        "step_run_id": step.step_run_id,
        "workflow_run_id": step.workflow_run_id,
        "step_key": step.step_key,
        "owner_role": step.owner_role,
        "status": step.status.value,
        "metadata": step.metadata,
        "started_at": None if step.started_at is None else step.started_at.isoformat(),
        "completed_at": None if step.completed_at is None else step.completed_at.isoformat(),
        "created_at": step.created_at.isoformat(),
        "updated_at": step.updated_at.isoformat(),
    }


def _work_order_payload(work_order: Any) -> dict[str, Any]:
    return {
        "work_order_id": work_order.work_order_id,
        "workflow_run_id": work_order.workflow_run_id,
        "step_run_id": work_order.step_run_id,
        "owner_role": work_order.owner_role,
        "task": work_order.task,
        "status": work_order.status.value,
        "required_outputs": work_order.required_outputs,
        "inputs": work_order.inputs,
        "timeout_seconds": work_order.timeout_seconds,
        "metadata": work_order.metadata,
        "created_at": work_order.created_at.isoformat(),
        "updated_at": work_order.updated_at.isoformat(),
    }


def _model_payload(item: BaseModel) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)  # type: ignore[attr-defined]
    return item.dict(exclude_none=True)


def _backlog_item_payload(items: list[SprintBacklogItem]) -> list[dict[str, Any]]:
    return [_model_payload(item) for item in items]


if APIRouter is not None:
    router = APIRouter(
        prefix="/v1",
        tags=["orchestration"],
        dependencies=[Depends(_require_api_key)],
    )
else:  # pragma: no cover
    router = None


@router.get("/workflow-definitions")  # type: ignore[union-attr]
def list_workflow_definitions(
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    return {"workflow_definitions": service.list_workflow_definitions()}


@router.post("/workflows/factory-scrum")  # type: ignore[union-attr]
def create_factory_scrum_project(
    body: FactoryScrumProjectRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        result = service.create_factory_scrum_project(
            title=body.title,
            objective=body.objective,
            created_by=body.created_by,
            project_id=body.project_id,
            branch_id=body.branch_id,
            complexity=body.complexity,
            autonomy_level=body.autonomy_level,
            sprint_goal=body.sprint_goal,
            backlog_items=_backlog_item_payload(body.backlog_items),
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    workflow_run = result["workflow_run"]
    sprint = result["sprint"]
    return {
        "workflow_run": _workflow_run_payload(workflow_run),
        "sprint": None
        if sprint is None
        else {
            "sprint_id": sprint["sprint_id"],
            "step_run_id": sprint["step_run_id"],
            "work_orders": [
                _work_order_payload(item) for item in sprint["work_orders"]
            ],
        },
        "kanban": result["kanban"],
    }


@router.post("/workflow-runs")  # type: ignore[union-attr]
def create_workflow_run(
    body: CreateWorkflowRunRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    run = service.create_workflow_run(
        workflow_definition_id=body.workflow_definition_id,
        workflow_version=body.workflow_version,
        title=body.title,
        created_by=body.created_by,
        initial_step_key=body.initial_step_key,
        initial_owner_role=body.initial_owner_role,
        description=body.description,
        metadata=body.metadata,
    )
    return {
        "workflow_run_id": run.workflow_run_id,
        "current_step_id": run.current_step_id,
        "workflow_run": _workflow_run_payload(run),
        "kanban": service.get_kanban_projection(run.workflow_run_id),
    }


@router.get("/workflow-runs")  # type: ignore[union-attr]
def list_workflow_runs(
    limit: int = 50,
    status: str | None = None,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    normalized_status = status.strip().lower() if status else None
    return {
        "workflow_runs": [
            _workflow_run_payload(item)
            for item in service.list_workflow_runs(
                limit=limit,
                status=normalized_status or None,
            )
        ],
    }


@router.get("/workflow-runs/{workflow_run_id}")  # type: ignore[union-attr]
def get_workflow_run(
    workflow_run_id: str,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    run = service.get_workflow_run(workflow_run_id)
    return _workflow_run_payload(run)


@router.get("/workflow-runs/{workflow_run_id}/steps")  # type: ignore[union-attr]
def list_steps(
    workflow_run_id: str,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    return {
        "workflow_run_id": workflow_run_id,
        "steps": [
            _step_payload(item)
            for item in service.list_step_runs(workflow_run_id)
        ],
    }


@router.get("/workflow-runs/{workflow_run_id}/work-orders")  # type: ignore[union-attr]
def list_work_orders(
    workflow_run_id: str,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    return {
        "workflow_run_id": workflow_run_id,
        "work_orders": [
            _work_order_payload(item)
            for item in service.list_work_orders(workflow_run_id)
        ],
    }


@router.get("/workflow-runs/{workflow_run_id}/kanban")  # type: ignore[union-attr]
def get_workflow_kanban(
    workflow_run_id: str,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    return service.get_kanban_projection(workflow_run_id)


@router.post("/workflow-runs/{workflow_run_id}/sprints")  # type: ignore[union-attr]
def open_sprint(
    workflow_run_id: str,
    body: OpenSprintRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        result = service.open_sprint(
            workflow_run_id=workflow_run_id,
            sprint_id=body.sprint_id,
            sprint_goal=body.sprint_goal,
            backlog_items=_backlog_item_payload(body.backlog_items),
            actor=body.actor,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "sprint_id": result["sprint_id"],
        "step_run_id": result["step_run_id"],
        "work_orders": [
            _work_order_payload(item) for item in result["work_orders"]
        ],
        "kanban": result["kanban"],
    }


@router.post("/workflow-runs/{workflow_run_id}/sprints/{sprint_id}/close")  # type: ignore[union-attr]
def close_sprint(
    workflow_run_id: str,
    sprint_id: str,
    body: CloseSprintRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        result = service.close_sprint(
            workflow_run_id=workflow_run_id,
            sprint_id=sprint_id,
            actor=body.actor,
            review_notes=body.review_notes,
            retrospective_notes=body.retrospective_notes,
            force=body.force,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "workflow_run": _workflow_run_payload(result["workflow_run"]),
        "sprint_id": result["sprint_id"],
        "unfinished_work_orders": result["unfinished_work_orders"],
        "kanban": result["kanban"],
    }


@router.get("/workflow-runs/{workflow_run_id}/timeline")  # type: ignore[union-attr]
def get_timeline(
    workflow_run_id: str,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    events = service.get_timeline(workflow_run_id)
    return {
        "workflow_run_id": workflow_run_id,
        "events": [
            {
                "event_id": item.event_id,
                "event_type": item.event_type,
                "actor": item.actor,
                "payload": item.payload,
                "step_run_id": item.step_run_id,
                "work_order_id": item.work_order_id,
                "created_at": item.created_at.isoformat(),
            }
            for item in events
        ],
    }


@router.post("/workflow-runs/{workflow_run_id}/work-orders")  # type: ignore[union-attr]
def create_work_order(
    workflow_run_id: str,
    body: CreateWorkOrderRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        work_order = service.create_work_order(
            workflow_run_id=workflow_run_id,
            step_run_id=body.step_run_id,
            owner_role=body.owner_role,
            task=body.task,
            required_outputs=body.required_outputs,
            inputs=body.inputs,
            timeout_seconds=body.timeout_seconds,
            actor=body.actor,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "work_order": _work_order_payload(work_order),
        "kanban": service.get_kanban_projection(workflow_run_id),
    }


@router.post("/work-orders/{work_order_id}/dispatch")  # type: ignore[union-attr]
def dispatch_work_order(
    work_order_id: str,
    body: HeartbeatRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        work_order = service.dispatch_work_order(work_order_id, actor=body.actor)
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "work_order": _work_order_payload(work_order),
        "kanban": service.get_kanban_projection(work_order.workflow_run_id),
    }


@router.post("/work-orders/{work_order_id}/heartbeat")  # type: ignore[union-attr]
def heartbeat_work_order(
    work_order_id: str,
    body: HeartbeatRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        work_order = service.heartbeat_work_order(
            work_order_id=work_order_id,
            actor=body.actor,
            metrics=body.metrics,
            notes=body.notes,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "work_order": _work_order_payload(work_order),
        "kanban": service.get_kanban_projection(work_order.workflow_run_id),
    }


@router.post("/work-orders/{work_order_id}/callback")  # type: ignore[union-attr]
def work_order_callback(
    work_order_id: str,
    body: WorkOrderCallbackRequest,
    request: Request,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header required")
    existing = service._repo.get_work_order(work_order_id)
    callback = WorkOrderCallback(
        work_order_id=work_order_id,
        attempt_id=body.attempt_id,
        status=body.status,
        actor=body.actor,
        commit_sha=body.commit_sha,
        changed_files=body.changed_files,
        artifact_refs=[
            ArtifactRef(
                artifact_ref_id=item.artifact_ref_id or new_id("artifact"),
                workflow_run_id=existing.workflow_run_id,
                step_run_id=item.step_run_id or existing.step_run_id,
                work_order_id=work_order_id,
                artifact_type=item.artifact_type,
                uri=item.uri,
                sha256=item.sha256,
                produced_by=item.produced_by,
                metadata=item.metadata,
            )
            for item in body.artifact_refs
        ],
        agent_logs=[
            AgentObservabilityLog(
                log_id=item.log_id or new_id("agent_log"),
                workflow_run_id=existing.workflow_run_id,
                step_run_id=item.step_run_id or existing.step_run_id,
                work_order_id=work_order_id,
                agent_id=item.agent_id,
                iteration=item.iteration,
                uri=item.uri,
                sha256=item.sha256,
                summary=item.summary,
                metadata=item.metadata,
            )
            for item in body.agent_logs
        ],
        test_results=body.test_results,
        blockers=body.blockers,
        metrics=body.metrics,
        notes=body.notes,
    )
    try:
        work_order = service.record_worker_callback(
            callback,
            idempotency_key=idempotency_key,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "work_order_id": work_order.work_order_id,
        "status": work_order.status.value,
        "work_order": _work_order_payload(work_order),
        "kanban": service.get_kanban_projection(work_order.workflow_run_id),
    }


@router.post("/workflow-runs/{workflow_run_id}/gates")  # type: ignore[union-attr]
def request_gate_review(
    workflow_run_id: str,
    body: GateReviewRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        gate = service.request_gate_review(
            workflow_run_id=workflow_run_id,
            step_run_id=body.step_run_id,
            requested_by=body.requested_by,
            reviewer_role=body.reviewer_role,
            evidence_refs=body.evidence_refs,
            notes=body.notes,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "gate_review_id": gate.gate_review_id,
        "status": gate.status.value,
        "kanban": service.get_kanban_projection(workflow_run_id),
    }


@router.post("/gates/{gate_review_id}/decision")  # type: ignore[union-attr]
def decide_gate(
    gate_review_id: str,
    body: GateDecisionRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        gate = service.decide_gate(
            gate_review_id=gate_review_id,
            decision=body.decision,
            reviewer_role=body.reviewer_role,
            notes=body.notes,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "gate_review_id": gate.gate_review_id,
        "status": gate.status.value,
        "kanban": service.get_kanban_projection(gate.workflow_run_id),
    }


@router.post("/workflow-runs/{workflow_run_id}/interventions")  # type: ignore[union-attr]
def request_intervention(
    workflow_run_id: str,
    body: InterventionRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    event = service.request_zeus_intervention(
        workflow_run_id=workflow_run_id,
        reason=body.reason,
        actor=body.actor,
        work_order_id=body.work_order_id,
        action=body.action,
        notes=body.notes,
    )
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "kanban": service.get_kanban_projection(workflow_run_id),
    }


@router.post("/workflow-runs/{workflow_run_id}/interventions/resolve")  # type: ignore[union-attr]
def resolve_intervention(
    workflow_run_id: str,
    body: ResolveInterventionRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    try:
        event = service.resolve_zeus_intervention(
            workflow_run_id=workflow_run_id,
            actor=body.actor,
            reason=body.reason,
            outcome=body.outcome,
            notes=body.notes,
            work_order_id=body.work_order_id,
        )
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "workflow_run": _workflow_run_payload(
            service.get_workflow_run(workflow_run_id)
        ),
        "kanban": service.get_kanban_projection(workflow_run_id),
    }


@router.post("/watchdog/run")  # type: ignore[union-attr]
def run_watchdog(
    body: WatchdogRunRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    return service.run_watchdog(actor=body.actor)


@router.post("/webhooks/factory-event")  # type: ignore[union-attr]
def ingest_factory_event(
    body: FactoryEventRequest,
    service: OrchestrationService = Depends(_get_service),
) -> dict[str, Any]:
    event = service.ingest_external_event(
        workflow_run_id=body.workflow_run_id,
        event_type=body.event_type,
        actor=body.actor,
        payload=body.payload,
        idempotency_key=body.idempotency_key,
        step_run_id=body.step_run_id,
        work_order_id=body.work_order_id,
    )
    return {
        "status": "duplicate" if event is None else "accepted",
        "event_id": None if event is None else event.event_id,
    }

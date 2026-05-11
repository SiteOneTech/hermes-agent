"""Repository ports and in-memory implementation for orchestration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol

from orchestration.domain import (
    AgentObservabilityLog,
    ArtifactRef,
    GateReview,
    WorkflowEvent,
    WorkflowRun,
    WorkOrder,
    StepRun,
)


class DuplicateInboxMessage(Exception):
    """Raised when an idempotent callback was already processed."""


class EntityNotFound(KeyError):
    """Raised when a required orchestration entity is missing."""


class OrchestrationRepository(Protocol):
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
    ) -> None: ...
    def list_workflow_definitions(self) -> list[dict]: ...

    def create_workflow_run(self, run: WorkflowRun) -> None: ...
    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun: ...
    def update_workflow_run(self, run: WorkflowRun) -> None: ...

    def create_step_run(self, step: StepRun) -> None: ...
    def get_step_run(self, step_run_id: str) -> StepRun: ...
    def list_step_runs(self, workflow_run_id: str) -> list[StepRun]: ...
    def update_step_run(self, step: StepRun) -> None: ...

    def create_work_order(self, work_order: WorkOrder) -> None: ...
    def get_work_order(self, work_order_id: str) -> WorkOrder: ...
    def list_work_orders(self, workflow_run_id: str) -> list[WorkOrder]: ...
    def list_stale_work_orders(self, now: datetime) -> list[WorkOrder]: ...
    def update_work_order(self, work_order: WorkOrder) -> None: ...

    def save_gate_review(self, gate: GateReview) -> None: ...
    def get_gate_review(self, gate_review_id: str) -> GateReview: ...
    def update_gate_review(self, gate: GateReview) -> None: ...

    def append_event(self, event: WorkflowEvent) -> None: ...
    def list_events(self, workflow_run_id: str) -> list[WorkflowEvent]: ...

    def add_artifact_ref(self, artifact: ArtifactRef) -> None: ...
    def add_agent_log(self, log: AgentObservabilityLog) -> None: ...

    def record_inbox_message(self, idempotency_key: str, payload: dict) -> bool: ...
    def upsert_kanban_projection(
        self,
        *,
        scope_type: str,
        scope_id: str,
        board_name: str,
        projection_json: dict,
        source_event_id: str | None = None,
    ) -> None: ...
    def get_kanban_projection(
        self,
        *,
        scope_type: str,
        scope_id: str,
        board_name: str,
    ) -> dict[str, Any] | None: ...


class InMemoryOrchestrationRepository:
    """Small deterministic repository for domain tests and fake-worker pilots."""

    def __init__(self) -> None:
        self.workflow_runs: dict[str, WorkflowRun] = {}
        self.workflow_definitions: dict[tuple[str, str], dict] = {}
        self.step_runs: dict[str, StepRun] = {}
        self.work_orders: dict[str, WorkOrder] = {}
        self.gate_reviews: dict[str, GateReview] = {}
        self.events: list[WorkflowEvent] = []
        self.artifacts: dict[str, ArtifactRef] = {}
        self.agent_logs: dict[str, AgentObservabilityLog] = {}
        self.inbox_keys: set[str] = set()
        self.kanban_projections: dict[tuple[str, str, str], dict[str, Any]] = {}

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
        self.workflow_definitions[(workflow_definition_id, workflow_version)] = {
            "workflow_definition_id": workflow_definition_id,
            "domain": domain,
            "display_name": display_name,
            "description": description,
            "workflow_version": workflow_version,
            "definition_json": deepcopy(definition_json),
            "status": status,
        }

    def list_workflow_definitions(self) -> list[dict]:
        return [deepcopy(value) for value in self.workflow_definitions.values()]

    def create_workflow_run(self, run: WorkflowRun) -> None:
        self.workflow_runs[run.workflow_run_id] = deepcopy(run)

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        try:
            return deepcopy(self.workflow_runs[workflow_run_id])
        except KeyError as exc:
            raise EntityNotFound(workflow_run_id) from exc

    def update_workflow_run(self, run: WorkflowRun) -> None:
        if run.workflow_run_id not in self.workflow_runs:
            raise EntityNotFound(run.workflow_run_id)
        self.workflow_runs[run.workflow_run_id] = deepcopy(run)

    def create_step_run(self, step: StepRun) -> None:
        self.step_runs[step.step_run_id] = deepcopy(step)

    def get_step_run(self, step_run_id: str) -> StepRun:
        try:
            return deepcopy(self.step_runs[step_run_id])
        except KeyError as exc:
            raise EntityNotFound(step_run_id) from exc

    def list_step_runs(self, workflow_run_id: str) -> list[StepRun]:
        return [
            deepcopy(item)
            for item in self.step_runs.values()
            if item.workflow_run_id == workflow_run_id
        ]

    def update_step_run(self, step: StepRun) -> None:
        if step.step_run_id not in self.step_runs:
            raise EntityNotFound(step.step_run_id)
        self.step_runs[step.step_run_id] = deepcopy(step)

    def create_work_order(self, work_order: WorkOrder) -> None:
        self.work_orders[work_order.work_order_id] = deepcopy(work_order)

    def get_work_order(self, work_order_id: str) -> WorkOrder:
        try:
            return deepcopy(self.work_orders[work_order_id])
        except KeyError as exc:
            raise EntityNotFound(work_order_id) from exc

    def list_work_orders(self, workflow_run_id: str) -> list[WorkOrder]:
        return [
            deepcopy(item)
            for item in self.work_orders.values()
            if item.workflow_run_id == workflow_run_id
        ]

    def list_stale_work_orders(self, now: datetime) -> list[WorkOrder]:
        stale = []
        for item in self.work_orders.values():
            if item.status.value not in {"pending", "dispatched", "running"}:
                continue
            deadline = item.updated_at.timestamp() + item.timeout_seconds
            if deadline <= now.timestamp():
                stale.append(deepcopy(item))
        return stale

    def update_work_order(self, work_order: WorkOrder) -> None:
        if work_order.work_order_id not in self.work_orders:
            raise EntityNotFound(work_order.work_order_id)
        self.work_orders[work_order.work_order_id] = deepcopy(work_order)

    def save_gate_review(self, gate: GateReview) -> None:
        self.gate_reviews[gate.gate_review_id] = deepcopy(gate)

    def get_gate_review(self, gate_review_id: str) -> GateReview:
        try:
            return deepcopy(self.gate_reviews[gate_review_id])
        except KeyError as exc:
            raise EntityNotFound(gate_review_id) from exc

    def update_gate_review(self, gate: GateReview) -> None:
        if gate.gate_review_id not in self.gate_reviews:
            raise EntityNotFound(gate.gate_review_id)
        self.gate_reviews[gate.gate_review_id] = deepcopy(gate)

    def append_event(self, event: WorkflowEvent) -> None:
        self.events.append(deepcopy(event))

    def list_events(self, workflow_run_id: str) -> list[WorkflowEvent]:
        return [
            deepcopy(item)
            for item in self.events
            if item.workflow_run_id == workflow_run_id
        ]

    def add_artifact_ref(self, artifact: ArtifactRef) -> None:
        self.artifacts[artifact.artifact_ref_id] = deepcopy(artifact)

    def add_agent_log(self, log: AgentObservabilityLog) -> None:
        self.agent_logs[log.log_id] = deepcopy(log)

    def record_inbox_message(self, idempotency_key: str, payload: dict) -> bool:
        if idempotency_key in self.inbox_keys:
            return False
        self.inbox_keys.add(idempotency_key)
        return True

    def upsert_kanban_projection(
        self,
        *,
        scope_type: str,
        scope_id: str,
        board_name: str,
        projection_json: dict,
        source_event_id: str | None = None,
    ) -> None:
        self.kanban_projections[(scope_type, scope_id, board_name)] = {
            "projection_json": deepcopy(projection_json),
            "source_event_id": source_event_id,
        }

    def get_kanban_projection(
        self,
        *,
        scope_type: str,
        scope_id: str,
        board_name: str,
    ) -> dict[str, Any] | None:
        projection = self.kanban_projections.get((scope_type, scope_id, board_name))
        if projection is None:
            return None
        return deepcopy(projection["projection_json"])

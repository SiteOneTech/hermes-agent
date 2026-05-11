"""Application service for Hermes orchestration."""

from __future__ import annotations

from typing import Any

from orchestration.domain import (
    AgentObservabilityLog,
    ArtifactRef,
    GateDecision,
    GateReview,
    GateStatus,
    StepRun,
    StepStatus,
    WorkflowEvent,
    WorkflowRun,
    WorkflowRunStatus,
    WorkOrder,
    WorkOrderCallback,
    WorkOrderStatus,
    new_id,
    utc_now,
)
from orchestration.projections import build_kanban_projection
from orchestration.repository import OrchestrationRepository
from orchestration.workflow_packs import get_workflow_pack, list_workflow_packs


class OrchestrationError(ValueError):
    """Raised when a workflow command violates orchestration rules."""


class OrchestrationService:
    """Coordinates state transitions without knowing storage details."""

    def __init__(self, repository: OrchestrationRepository) -> None:
        self._repo = repository
        self.publish_builtin_workflows()

    def publish_builtin_workflows(self) -> None:
        for pack in list_workflow_packs():
            self._repo.upsert_workflow_definition(
                workflow_definition_id=pack.workflow_definition_id,
                domain=pack.domain,
                display_name=pack.display_name,
                description=pack.description,
                workflow_version=pack.version,
                definition_json=pack.to_definition_json(),
                status="published",
            )

    def list_workflow_definitions(self) -> list[dict]:
        return self._repo.list_workflow_definitions()

    def create_workflow_run(
        self,
        *,
        workflow_definition_id: str,
        workflow_version: str,
        title: str,
        created_by: str,
        initial_step_key: str = "INTAKE",
        initial_owner_role: str = "zeus",
        metadata: dict | None = None,
    ) -> WorkflowRun:
        pack = get_workflow_pack(workflow_definition_id, workflow_version)
        if pack is not None:
            if initial_step_key == "INTAKE":
                initial_step_key = pack.initial_step_key
            if initial_owner_role == "zeus":
                initial_owner_role = pack.initial_owner_role
            metadata = {
                "methodology": pack.methodology,
                "workflow_domain": pack.domain,
                "workflow_display_name": pack.display_name,
                **(metadata or {}),
            }
        run = WorkflowRun(
            workflow_run_id=new_id("wf_run"),
            workflow_definition_id=workflow_definition_id,
            workflow_version=workflow_version,
            title=title,
            status=WorkflowRunStatus.ACTIVE,
            current_step_id=None,
            created_by=created_by,
            metadata=metadata or {},
        )
        step = StepRun(
            step_run_id=new_id("step"),
            workflow_run_id=run.workflow_run_id,
            step_key=initial_step_key,
            owner_role=initial_owner_role,
            status=StepStatus.RUNNING,
            started_at=utc_now(),
        )
        self._repo.create_workflow_run(run)
        self._repo.create_step_run(step)
        run.current_step_id = step.step_run_id
        run.updated_at = utc_now()
        self._repo.update_workflow_run(run)
        self._append_event(
            run.workflow_run_id,
            "workflow.run.created",
            created_by,
            {
                "workflow_definition_id": workflow_definition_id,
                "workflow_version": workflow_version,
                "title": title,
                "initial_step_key": initial_step_key,
                "methodology": None if pack is None else pack.methodology,
            },
            step_run_id=step.step_run_id,
        )
        self.refresh_kanban_projection(run.workflow_run_id)
        return run

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        return self._repo.get_workflow_run(workflow_run_id)

    def list_workflow_runs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[WorkflowRun]:
        return self._repo.list_workflow_runs(limit=limit, status=status)

    def get_timeline(self, workflow_run_id: str) -> list[WorkflowEvent]:
        return self._repo.list_events(workflow_run_id)

    def list_step_runs(self, workflow_run_id: str) -> list[StepRun]:
        return self._repo.list_step_runs(workflow_run_id)

    def list_work_orders(self, workflow_run_id: str) -> list[WorkOrder]:
        return self._repo.list_work_orders(workflow_run_id)

    def create_factory_scrum_project(
        self,
        *,
        title: str,
        objective: str,
        created_by: str,
        project_id: str,
        branch_id: str = "sicilia",
        complexity: str = "standard",
        autonomy_level: str = "L2",
        backlog_items: list[dict[str, Any]] | None = None,
        sprint_goal: str = "",
    ) -> dict[str, Any]:
        run = self.create_workflow_run(
            workflow_definition_id="factory.scrum_project",
            workflow_version="1.0.0",
            title=title,
            created_by=created_by,
            metadata={
                "project_id": project_id,
                "branch_id": branch_id,
                "objective": objective,
                "complexity": complexity,
                "autonomy_level": autonomy_level,
                "source": "orchestration.factory_scrum_project",
            },
        )
        opened_sprint: dict[str, Any] | None = None
        if backlog_items:
            opened_sprint = self.open_sprint(
                workflow_run_id=run.workflow_run_id,
                sprint_id="sprint-001",
                sprint_goal=sprint_goal or f"First increment for {title}",
                backlog_items=backlog_items,
                actor=created_by,
            )
            run = self._repo.get_workflow_run(run.workflow_run_id)
        return {
            "workflow_run": run,
            "sprint": opened_sprint,
            "kanban": self.refresh_kanban_projection(run.workflow_run_id),
        }

    def open_sprint(
        self,
        *,
        workflow_run_id: str,
        sprint_id: str,
        sprint_goal: str,
        backlog_items: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        run = self._repo.get_workflow_run(workflow_run_id)
        now = utc_now()
        if run.current_step_id:
            current_step = self._repo.get_step_run(run.current_step_id)
            if current_step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                current_step.status = StepStatus.COMPLETED
                current_step.completed_at = now
                current_step.updated_at = now
                current_step.metadata = {
                    **current_step.metadata,
                    "completed_by": actor,
                    "completion_reason": "sprint_opened",
                    "next_sprint_id": sprint_id,
                }
                self._repo.update_step_run(current_step)
                self._append_event(
                    workflow_run_id,
                    "step.completed",
                    actor,
                    {
                        "step_key": current_step.step_key,
                        "completion_reason": "sprint_opened",
                        "next_sprint_id": sprint_id,
                    },
                    step_run_id=current_step.step_run_id,
                )
        sprint_step = StepRun(
            step_run_id=new_id("step"),
            workflow_run_id=workflow_run_id,
            step_key="SPRINT_EXECUTION",
            owner_role="leo-orquestador",
            status=StepStatus.RUNNING,
            metadata={
                "sprint_id": sprint_id,
                "sprint_goal": sprint_goal,
                "scrum_event": "sprint_kickoff",
            },
            started_at=now,
        )
        self._repo.create_step_run(sprint_step)
        run.current_step_id = sprint_step.step_run_id
        run.status = WorkflowRunStatus.ACTIVE
        run.metadata = {
            **run.metadata,
            "current_sprint_id": sprint_id,
            "sprints": {
                **(
                    run.metadata.get("sprints", {})
                    if isinstance(run.metadata.get("sprints"), dict)
                    else {}
                ),
                sprint_id: {
                    "status": "active",
                    "goal": sprint_goal,
                    "opened_at": now.isoformat(),
                    "backlog_count": len(backlog_items),
                },
            },
        }
        run.updated_at = now
        self._repo.update_workflow_run(run)
        self._append_event(
            workflow_run_id,
            "sprint.opened",
            actor,
            {
                "sprint_id": sprint_id,
                "sprint_goal": sprint_goal,
                "backlog_count": len(backlog_items),
            },
            step_run_id=sprint_step.step_run_id,
        )
        work_orders = []
        for index, item in enumerate(backlog_items, start=1):
            owner_role = str(item.get("owner_role") or item.get("owner") or "factory-stage-owner")
            task = str(item.get("task") or item.get("title") or f"Sprint item {index}")
            required_outputs = item.get("required_outputs")
            if not isinstance(required_outputs, list):
                required_outputs = ["artifact_ref", "agent_log_ref"]
            inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
            timeout_seconds = (
                int(item["timeout_seconds"])
                if item.get("timeout_seconds") is not None
                else 1800
            )
            work_order = WorkOrder(
                work_order_id=str(item.get("work_order_id") or new_id("wo")),
                workflow_run_id=workflow_run_id,
                step_run_id=sprint_step.step_run_id,
                owner_role=owner_role,
                task=task,
                status=WorkOrderStatus.PENDING,
                required_outputs=required_outputs,
                inputs={
                    **inputs,
                    "sprint_id": sprint_id,
                    "backlog_item_id": item.get("backlog_item_id") or f"{sprint_id}-{index:02d}",
                },
                timeout_seconds=timeout_seconds,
                metadata={
                    "sprint_id": sprint_id,
                    "backlog_item_id": item.get("backlog_item_id") or f"{sprint_id}-{index:02d}",
                    "expected_first_heartbeat_seconds": int(
                        item.get("expected_first_heartbeat_seconds") or min(timeout_seconds, 900)
                    ),
                    "retry_policy": item.get("retry_policy") or {"max_attempts": 1},
                },
            )
            self._repo.create_work_order(work_order)
            work_orders.append(work_order)
            self._append_event(
                workflow_run_id,
                "work_order.created",
                actor,
                {
                    "sprint_id": sprint_id,
                    "owner_role": owner_role,
                    "required_outputs": required_outputs,
                    "timeout_seconds": timeout_seconds,
                },
                step_run_id=sprint_step.step_run_id,
                work_order_id=work_order.work_order_id,
            )
        kanban = self.refresh_kanban_projection(workflow_run_id)
        return {
            "sprint_id": sprint_id,
            "step_run_id": sprint_step.step_run_id,
            "work_orders": work_orders,
            "kanban": kanban,
        }

    def create_work_order(
        self,
        *,
        workflow_run_id: str,
        step_run_id: str,
        owner_role: str,
        task: str,
        required_outputs: list[str] | None = None,
        inputs: dict | None = None,
        timeout_seconds: int = 1800,
        actor: str = "leo-orquestador",
    ) -> WorkOrder:
        run = self._repo.get_workflow_run(workflow_run_id)
        step = self._repo.get_step_run(step_run_id)
        if step.workflow_run_id != run.workflow_run_id:
            raise OrchestrationError("Step does not belong to workflow run")
        if step.status not in {StepStatus.RUNNING, StepStatus.PENDING}:
            raise OrchestrationError(f"Cannot add work order while step is {step.status}")

        work_order = WorkOrder(
            work_order_id=new_id("wo"),
            workflow_run_id=workflow_run_id,
            step_run_id=step_run_id,
            owner_role=owner_role,
            task=task,
            status=WorkOrderStatus.PENDING,
            required_outputs=required_outputs or [],
            inputs=inputs or {},
            timeout_seconds=timeout_seconds,
        )
        self._repo.create_work_order(work_order)
        self._append_event(
            workflow_run_id,
            "work_order.created",
            actor,
            {
                "owner_role": owner_role,
                "required_outputs": work_order.required_outputs,
                "timeout_seconds": timeout_seconds,
            },
            step_run_id=step_run_id,
            work_order_id=work_order.work_order_id,
        )
        self.refresh_kanban_projection(workflow_run_id)
        return work_order

    def dispatch_work_order(self, work_order_id: str, *, actor: str) -> WorkOrder:
        work_order = self._repo.get_work_order(work_order_id)
        if work_order.status != WorkOrderStatus.PENDING:
            raise OrchestrationError(
                f"Only pending work orders can be dispatched; got {work_order.status}"
            )
        work_order.status = WorkOrderStatus.DISPATCHED
        work_order.updated_at = utc_now()
        self._repo.update_work_order(work_order)
        self._append_event(
            work_order.workflow_run_id,
            "work_order.dispatched",
            actor,
            {"owner_role": work_order.owner_role},
            step_run_id=work_order.step_run_id,
            work_order_id=work_order.work_order_id,
        )
        self.refresh_kanban_projection(work_order.workflow_run_id)
        return work_order

    def record_worker_callback(
        self,
        callback: WorkOrderCallback,
        *,
        idempotency_key: str,
    ) -> WorkOrder:
        if not self._repo.record_inbox_message(
            idempotency_key,
            {
                "work_order_id": callback.work_order_id,
                "attempt_id": callback.attempt_id,
                "status": callback.status.value,
            },
        ):
            return self._repo.get_work_order(callback.work_order_id)

        work_order = self._repo.get_work_order(callback.work_order_id)
        if callback.status not in {
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.FAILED,
            WorkOrderStatus.RUNNING,
        }:
            raise OrchestrationError(f"Unsupported callback status: {callback.status}")

        work_order.status = callback.status
        work_order.metadata = {
            **work_order.metadata,
            "last_attempt_id": callback.attempt_id,
            "commit_sha": callback.commit_sha,
            "changed_files": callback.changed_files,
            "test_results": callback.test_results,
            "blockers": callback.blockers,
            "metrics": callback.metrics,
            "notes": callback.notes,
        }
        work_order.updated_at = utc_now()
        self._repo.update_work_order(work_order)

        for artifact in callback.artifact_refs:
            self._repo.add_artifact_ref(artifact)
        for log in callback.agent_logs:
            self._repo.add_agent_log(log)

        event_type = {
            WorkOrderStatus.COMPLETED: "work_order.completed",
            WorkOrderStatus.FAILED: "work_order.failed",
            WorkOrderStatus.RUNNING: "work_order.heartbeat",
        }[callback.status]
        self._append_event(
            work_order.workflow_run_id,
            event_type,
            callback.actor,
            {
                "attempt_id": callback.attempt_id,
                "commit_sha": callback.commit_sha,
                "changed_files": callback.changed_files,
                "artifact_refs": [
                    item.artifact_ref_id for item in callback.artifact_refs
                ],
                "agent_logs": [item.log_id for item in callback.agent_logs],
                "test_results": callback.test_results,
                "blockers": callback.blockers,
                "metrics": callback.metrics,
            },
            step_run_id=work_order.step_run_id,
            work_order_id=work_order.work_order_id,
            idempotency_key=idempotency_key,
        )
        self.refresh_kanban_projection(work_order.workflow_run_id)
        return work_order

    def request_gate_review(
        self,
        *,
        workflow_run_id: str,
        step_run_id: str,
        requested_by: str,
        reviewer_role: str,
        evidence_refs: list[str],
        notes: str = "",
    ) -> GateReview:
        run = self._repo.get_workflow_run(workflow_run_id)
        step = self._repo.get_step_run(step_run_id)
        if step.workflow_run_id != run.workflow_run_id:
            raise OrchestrationError("Step does not belong to workflow run")
        if step.status not in {StepStatus.RUNNING, StepStatus.HOLD}:
            raise OrchestrationError(f"Cannot request gate while step is {step.status}")

        gate = GateReview(
            gate_review_id=new_id("gate"),
            workflow_run_id=workflow_run_id,
            step_run_id=step_run_id,
            requested_by=requested_by,
            reviewer_role=reviewer_role,
            status=GateStatus.PENDING,
            evidence_refs=evidence_refs,
            notes=notes,
        )
        step.status = StepStatus.WAITING_GATE
        step.updated_at = utc_now()
        run.status = WorkflowRunStatus.WAITING_GATE
        run.updated_at = utc_now()
        self._repo.update_step_run(step)
        self._repo.update_workflow_run(run)
        self._repo.save_gate_review(gate)
        self._append_event(
            workflow_run_id,
            "gate.review_requested",
            requested_by,
            {
                "gate_review_id": gate.gate_review_id,
                "reviewer_role": reviewer_role,
                "evidence_refs": evidence_refs,
                "notes": notes,
            },
            step_run_id=step_run_id,
        )
        self.refresh_kanban_projection(workflow_run_id)
        return gate

    def decide_gate(
        self,
        *,
        gate_review_id: str,
        decision: GateDecision,
        reviewer_role: str,
        notes: str,
    ) -> GateReview:
        gate = self._repo.get_gate_review(gate_review_id)
        if gate.reviewer_role != reviewer_role:
            raise OrchestrationError(
                f"Gate reviewer mismatch: expected {gate.reviewer_role}, got {reviewer_role}"
            )
        if gate.status != GateStatus.PENDING:
            raise OrchestrationError(f"Gate is already decided: {gate.status}")

        run = self._repo.get_workflow_run(gate.workflow_run_id)
        step = self._repo.get_step_run(gate.step_run_id)
        now = utc_now()
        gate.decision_by = reviewer_role
        gate.decision_notes = notes
        gate.decided_at = now
        gate.updated_at = now

        if decision == GateDecision.APPROVE:
            gate.status = GateStatus.APPROVED
            step.status = StepStatus.COMPLETED
            step.completed_at = now
            run.status = WorkflowRunStatus.ACTIVE
        elif decision == GateDecision.HOLD:
            gate.status = GateStatus.HOLD
            step.status = StepStatus.HOLD
            run.status = WorkflowRunStatus.HOLD
        elif decision == GateDecision.CHANGES_REQUESTED:
            gate.status = GateStatus.CHANGES_REQUESTED
            step.status = StepStatus.CHANGES_REQUESTED
            run.status = WorkflowRunStatus.HOLD
        elif decision == GateDecision.REJECT:
            gate.status = GateStatus.REJECTED
            step.status = StepStatus.FAILED
            run.status = WorkflowRunStatus.BLOCKED
        else:
            raise OrchestrationError(f"Unsupported gate decision: {decision}")

        step.updated_at = now
        run.updated_at = now
        self._repo.update_gate_review(gate)
        self._repo.update_step_run(step)
        self._repo.update_workflow_run(run)
        self._append_event(
            run.workflow_run_id,
            "gate.decision_recorded",
            reviewer_role,
            {
                "gate_review_id": gate.gate_review_id,
                "decision": decision.value,
                "status": gate.status.value,
                "notes": notes,
            },
            step_run_id=step.step_run_id,
        )
        self.refresh_kanban_projection(run.workflow_run_id)
        return gate

    def ingest_external_event(
        self,
        *,
        workflow_run_id: str,
        event_type: str,
        actor: str,
        payload: dict,
        idempotency_key: str,
        step_run_id: str | None = None,
        work_order_id: str | None = None,
    ) -> WorkflowEvent | None:
        if not self._repo.record_inbox_message(idempotency_key, payload):
            return None
        event = self._append_event(
            workflow_run_id,
            event_type,
            actor,
            payload,
            step_run_id=step_run_id,
            work_order_id=work_order_id,
            idempotency_key=idempotency_key,
        )
        self.refresh_kanban_projection(workflow_run_id)
        return event

    def heartbeat_work_order(
        self,
        *,
        work_order_id: str,
        actor: str,
        metrics: dict[str, Any] | None = None,
        notes: str = "",
    ) -> WorkOrder:
        work_order = self._repo.get_work_order(work_order_id)
        if work_order.status in {
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.FAILED,
            WorkOrderStatus.TIMED_OUT,
            WorkOrderStatus.CANCELLED,
        }:
            raise OrchestrationError(f"Cannot heartbeat terminal work order {work_order.status}")
        work_order.status = WorkOrderStatus.RUNNING
        work_order.metadata = {
            **work_order.metadata,
            "last_heartbeat_at": utc_now().isoformat(),
            "last_heartbeat_actor": actor,
            "last_heartbeat_notes": notes,
            "last_heartbeat_metrics": metrics or {},
        }
        work_order.updated_at = utc_now()
        self._repo.update_work_order(work_order)
        self._append_event(
            work_order.workflow_run_id,
            "work_order.heartbeat",
            actor,
            {
                "metrics": metrics or {},
                "notes": notes,
            },
            step_run_id=work_order.step_run_id,
            work_order_id=work_order.work_order_id,
        )
        self.refresh_kanban_projection(work_order.workflow_run_id)
        return work_order

    def close_sprint(
        self,
        *,
        workflow_run_id: str,
        sprint_id: str,
        actor: str,
        review_notes: str,
        retrospective_notes: str,
        force: bool = False,
    ) -> dict[str, Any]:
        run = self._repo.get_workflow_run(workflow_run_id)
        work_orders = [
            item
            for item in self._repo.list_work_orders(workflow_run_id)
            if item.metadata.get("sprint_id") == sprint_id
        ]
        not_done = [
            item.work_order_id
            for item in work_orders
            if item.status
            not in {
                WorkOrderStatus.COMPLETED,
                WorkOrderStatus.CANCELLED,
            }
        ]
        if not_done and not force:
            raise OrchestrationError(
                f"Sprint {sprint_id} still has unfinished work orders: {', '.join(not_done)}"
            )

        now = utc_now()
        for step in self._repo.list_step_runs(workflow_run_id):
            if step.metadata.get("sprint_id") != sprint_id:
                continue
            step.status = StepStatus.COMPLETED if not not_done else StepStatus.HOLD
            step.completed_at = now if not not_done else None
            step.updated_at = now
            self._repo.update_step_run(step)

        sprints = run.metadata.get("sprints") if isinstance(run.metadata.get("sprints"), dict) else {}
        sprint = sprints.get(sprint_id, {}) if isinstance(sprints.get(sprint_id), dict) else {}
        sprints[sprint_id] = {
            **sprint,
            "status": "closed" if not not_done else "hold",
            "closed_at": now.isoformat(),
            "review_notes": review_notes,
            "retrospective_notes": retrospective_notes,
            "unfinished_work_orders": not_done,
        }
        run.metadata = {
            **run.metadata,
            "sprints": sprints,
            "last_closed_sprint_id": sprint_id,
        }
        run.status = WorkflowRunStatus.ACTIVE if not not_done else WorkflowRunStatus.HOLD
        run.updated_at = now
        self._repo.update_workflow_run(run)
        self._append_event(
            workflow_run_id,
            "sprint.review_completed",
            actor,
            {
                "sprint_id": sprint_id,
                "review_notes": review_notes,
                "unfinished_work_orders": not_done,
            },
        )
        self._append_event(
            workflow_run_id,
            "sprint.retrospective_recorded",
            actor,
            {
                "sprint_id": sprint_id,
                "retrospective_notes": retrospective_notes,
            },
        )
        return {
            "workflow_run": run,
            "sprint_id": sprint_id,
            "unfinished_work_orders": not_done,
            "kanban": self.refresh_kanban_projection(workflow_run_id),
        }

    def request_zeus_intervention(
        self,
        *,
        workflow_run_id: str,
        reason: str,
        actor: str = "zeus-watchdog",
        work_order_id: str | None = None,
        action: str = "inspect",
        notes: str = "",
    ) -> WorkflowEvent:
        run = self._repo.get_workflow_run(workflow_run_id)
        run.metadata = {
            **run.metadata,
            "last_intervention_required": {
                "reason": reason,
                "actor": actor,
                "action": action,
                "notes": notes,
                "work_order_id": work_order_id,
                "created_at": utc_now().isoformat(),
            },
        }
        run.status = WorkflowRunStatus.BLOCKED if action in {"timeout", "blocked"} else run.status
        run.updated_at = utc_now()
        self._repo.update_workflow_run(run)
        event = self._append_event(
            workflow_run_id,
            "zeus.intervention_required",
            actor,
            {
                "reason": reason,
                "action": action,
                "notes": notes,
                "work_order_id": work_order_id,
            },
            work_order_id=work_order_id,
        )
        self.refresh_kanban_projection(workflow_run_id)
        return event

    def run_watchdog(self, *, actor: str = "zeus-watchdog") -> dict[str, Any]:
        now = utc_now()
        timed_out: list[dict[str, Any]] = []
        for work_order in self._repo.list_stale_work_orders(now):
            run = self._repo.get_workflow_run(work_order.workflow_run_id)
            step = self._repo.get_step_run(work_order.step_run_id)
            work_order.status = WorkOrderStatus.TIMED_OUT
            work_order.metadata = {
                **work_order.metadata,
                "timeout_detected_at": now.isoformat(),
                "failure_category": "timeout",
                "requires_zeus_intervention": True,
            }
            work_order.updated_at = now
            self._repo.update_work_order(work_order)
            step.status = StepStatus.HOLD
            step.updated_at = now
            self._repo.update_step_run(step)
            run.status = WorkflowRunStatus.BLOCKED
            run.metadata = {
                **run.metadata,
                "last_timeout_work_order_id": work_order.work_order_id,
                "last_timeout_detected_at": now.isoformat(),
            }
            run.updated_at = now
            self._repo.update_workflow_run(run)
            self._append_event(
                work_order.workflow_run_id,
                "work_order.timeout_detected",
                actor,
                {
                    "work_order_id": work_order.work_order_id,
                    "timeout_seconds": work_order.timeout_seconds,
                    "owner_role": work_order.owner_role,
                },
                step_run_id=work_order.step_run_id,
                work_order_id=work_order.work_order_id,
            )
            self.request_zeus_intervention(
                workflow_run_id=work_order.workflow_run_id,
                reason=f"Work order {work_order.work_order_id} timed out.",
                actor=actor,
                work_order_id=work_order.work_order_id,
                action="timeout",
                notes="Inspect node logs, split/retry/reassign, and record learning.",
            )
            timed_out.append(
                {
                    "workflow_run_id": work_order.workflow_run_id,
                    "work_order_id": work_order.work_order_id,
                    "owner_role": work_order.owner_role,
                }
            )
            self.refresh_kanban_projection(work_order.workflow_run_id)
        return {
            "checked_at": now.isoformat(),
            "timed_out_count": len(timed_out),
            "timed_out": timed_out,
        }

    def refresh_kanban_projection(
        self,
        workflow_run_id: str,
        *,
        board_name: str = "workflow",
    ) -> dict[str, Any]:
        run = self._repo.get_workflow_run(workflow_run_id)
        projection = build_kanban_projection(
            run=run,
            steps=self._repo.list_step_runs(workflow_run_id),
            work_orders=self._repo.list_work_orders(workflow_run_id),
            events=self._repo.list_events(workflow_run_id),
            board_name=board_name,
        )
        last_event_id = None
        events = self._repo.list_events(workflow_run_id)
        if events:
            last_event_id = events[-1].event_id
        self._repo.upsert_kanban_projection(
            scope_type="workflow_run",
            scope_id=workflow_run_id,
            board_name=board_name,
            projection_json=projection,
            source_event_id=last_event_id,
        )
        return projection

    def get_kanban_projection(
        self,
        workflow_run_id: str,
        *,
        board_name: str = "workflow",
    ) -> dict[str, Any]:
        cached = self._repo.get_kanban_projection(
            scope_type="workflow_run",
            scope_id=workflow_run_id,
            board_name=board_name,
        )
        if cached is not None:
            return cached
        return self.refresh_kanban_projection(workflow_run_id, board_name=board_name)

    def _append_event(
        self,
        workflow_run_id: str,
        event_type: str,
        actor: str,
        payload: dict,
        *,
        step_run_id: str | None = None,
        work_order_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> WorkflowEvent:
        event = WorkflowEvent(
            event_id=new_id("evt"),
            workflow_run_id=workflow_run_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
            step_run_id=step_run_id,
            work_order_id=work_order_id,
            idempotency_key=idempotency_key,
        )
        self._repo.append_event(event)
        return event

"""Application service for Hermes orchestration."""

from __future__ import annotations

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
from orchestration.repository import OrchestrationRepository


class OrchestrationError(ValueError):
    """Raised when a workflow command violates orchestration rules."""


class OrchestrationService:
    """Coordinates state transitions without knowing storage details."""

    def __init__(self, repository: OrchestrationRepository) -> None:
        self._repo = repository

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
            },
            step_run_id=step.step_run_id,
        )
        return run

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        return self._repo.get_workflow_run(workflow_run_id)

    def get_timeline(self, workflow_run_id: str) -> list[WorkflowEvent]:
        return self._repo.list_events(workflow_run_id)

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
        return self._append_event(
            workflow_run_id,
            event_type,
            actor,
            payload,
            step_run_id=step_run_id,
            work_order_id=work_order_id,
            idempotency_key=idempotency_key,
        )

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

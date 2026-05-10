from orchestration.domain import (
    AgentObservabilityLog,
    ArtifactRef,
    GateDecision,
    GateStatus,
    StepStatus,
    WorkOrderCallback,
    WorkOrderStatus,
    new_id,
)
from orchestration.repository import InMemoryOrchestrationRepository
from orchestration.service import OrchestrationError, OrchestrationService


def make_service() -> OrchestrationService:
    return OrchestrationService(InMemoryOrchestrationRepository())


def test_create_run_records_initial_step_and_event():
    service = make_service()

    run = service.create_workflow_run(
        workflow_definition_id="software.simple_website.fast_lane",
        workflow_version="1.0.0",
        title="LOLO pilot",
        created_by="zeus",
    )

    assert run.workflow_run_id.startswith("wf_run_")
    assert run.current_step_id is not None
    timeline = service.get_timeline(run.workflow_run_id)
    assert [event.event_type for event in timeline] == ["workflow.run.created"]


def test_worker_callback_is_idempotent_and_links_observability_log():
    service = make_service()
    run = service.create_workflow_run(
        workflow_definition_id="software.simple_website.fast_lane",
        workflow_version="1.0.0",
        title="Fast lane",
        created_by="zeus",
    )
    work_order = service.create_work_order(
        workflow_run_id=run.workflow_run_id,
        step_run_id=run.current_step_id or "",
        owner_role="ciro-codex",
        task="Build the inquiry form",
        required_outputs=["commit_sha", "agent_log_ref"],
    )

    artifact = ArtifactRef(
        artifact_ref_id=new_id("artifact"),
        workflow_run_id=run.workflow_run_id,
        step_run_id=run.current_step_id,
        work_order_id=work_order.work_order_id,
        artifact_type="commit",
        uri="git:abc123",
        produced_by="ciro-codex",
    )
    log = AgentObservabilityLog(
        log_id=new_id("agent_log"),
        workflow_run_id=run.workflow_run_id,
        step_run_id=run.current_step_id,
        work_order_id=work_order.work_order_id,
        agent_id="ciro-codex",
        uri="agent_logs/ciro-codex/iteration-001.md",
        summary="Implemented inquiry form and tests.",
    )
    callback = WorkOrderCallback(
        work_order_id=work_order.work_order_id,
        attempt_id="attempt-1",
        status=WorkOrderStatus.COMPLETED,
        actor="ciro-codex",
        commit_sha="abc123",
        artifact_refs=[artifact],
        agent_logs=[log],
    )

    first = service.record_worker_callback(callback, idempotency_key="wo:attempt-1")
    second = service.record_worker_callback(callback, idempotency_key="wo:attempt-1")

    assert first.status == WorkOrderStatus.COMPLETED
    assert second.status == WorkOrderStatus.COMPLETED
    timeline = service.get_timeline(run.workflow_run_id)
    assert [event.event_type for event in timeline].count("work_order.completed") == 1
    completed = [event for event in timeline if event.event_type == "work_order.completed"][0]
    assert completed.payload["agent_logs"] == [log.log_id]


def test_gate_decision_controls_state_not_markdown_log():
    service = make_service()
    run = service.create_workflow_run(
        workflow_definition_id="software.product.multi_sprint",
        workflow_version="1.0.0",
        title="Backend product",
        created_by="zeus",
        initial_step_key="ARCHITECTURE_REVIEW",
        initial_owner_role="nico-arquitecto",
    )

    gate = service.request_gate_review(
        workflow_run_id=run.workflow_run_id,
        step_run_id=run.current_step_id or "",
        requested_by="nico-arquitecto",
        reviewer_role="zeus",
        evidence_refs=["artifact:architecture_brief"],
        notes="Architecture evidence ready.",
    )

    waiting_run = service.get_workflow_run(run.workflow_run_id)
    waiting_step = service._repo.get_step_run(run.current_step_id or "")
    assert waiting_run.status.value == "waiting_gate"
    assert waiting_step.status == StepStatus.WAITING_GATE

    decided = service.decide_gate(
        gate_review_id=gate.gate_review_id,
        decision=GateDecision.HOLD,
        reviewer_role="zeus",
        notes="Need a data retention decision.",
    )

    held_run = service.get_workflow_run(run.workflow_run_id)
    held_step = service._repo.get_step_run(run.current_step_id or "")
    assert decided.status == GateStatus.HOLD
    assert held_run.status.value == "hold"
    assert held_step.status == StepStatus.HOLD


def test_gate_rejects_wrong_reviewer():
    service = make_service()
    run = service.create_workflow_run(
        workflow_definition_id="software.product.multi_sprint",
        workflow_version="1.0.0",
        title="Backend product",
        created_by="zeus",
    )
    gate = service.request_gate_review(
        workflow_run_id=run.workflow_run_id,
        step_run_id=run.current_step_id or "",
        requested_by="ana-pmo",
        reviewer_role="zeus",
        evidence_refs=["artifact:intake"],
    )

    try:
        service.decide_gate(
            gate_review_id=gate.gate_review_id,
            decision=GateDecision.APPROVE,
            reviewer_role="leo-orquestador",
            notes="Wrong reviewer.",
        )
    except OrchestrationError as exc:
        assert "reviewer mismatch" in str(exc)
    else:
        raise AssertionError("Expected reviewer mismatch")

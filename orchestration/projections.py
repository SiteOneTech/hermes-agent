"""Kanban projections derived from durable orchestration state."""

from __future__ import annotations

from collections import Counter
from typing import Any

from orchestration.domain import (
    StepRun,
    StepStatus,
    WorkflowEvent,
    WorkflowRun,
    WorkflowRunStatus,
    WorkOrder,
    WorkOrderStatus,
)


KANBAN_SCHEMA = "hermes.orchestration_kanban.v1"
DEFAULT_COLUMNS = ("backlog", "ready", "running", "review", "hold", "blocked", "done")


def _run_column(status: WorkflowRunStatus) -> str:
    return {
        WorkflowRunStatus.ACTIVE: "running",
        WorkflowRunStatus.WAITING_GATE: "review",
        WorkflowRunStatus.HOLD: "hold",
        WorkflowRunStatus.BLOCKED: "blocked",
        WorkflowRunStatus.COMPLETED: "done",
        WorkflowRunStatus.CANCELLED: "done",
    }[status]


def _step_column(status: StepStatus) -> str:
    return {
        StepStatus.PENDING: "backlog",
        StepStatus.RUNNING: "running",
        StepStatus.WAITING_GATE: "review",
        StepStatus.HOLD: "hold",
        StepStatus.CHANGES_REQUESTED: "blocked",
        StepStatus.COMPLETED: "done",
        StepStatus.FAILED: "blocked",
        StepStatus.CANCELLED: "done",
    }[status]


def _work_order_column(status: WorkOrderStatus) -> str:
    return {
        WorkOrderStatus.PENDING: "backlog",
        WorkOrderStatus.DISPATCHED: "ready",
        WorkOrderStatus.RUNNING: "running",
        WorkOrderStatus.COMPLETED: "done",
        WorkOrderStatus.FAILED: "blocked",
        WorkOrderStatus.TIMED_OUT: "blocked",
        WorkOrderStatus.CANCELLED: "done",
    }[status]


def _latest_event_by_work_order(events: list[WorkflowEvent]) -> dict[str, WorkflowEvent]:
    latest: dict[str, WorkflowEvent] = {}
    for event in events:
        if not event.work_order_id:
            continue
        current = latest.get(event.work_order_id)
        if current is None or current.created_at <= event.created_at:
            latest[event.work_order_id] = event
    return latest


def _card(
    *,
    card_id: str,
    card_type: str,
    title: str,
    column: str,
    owner_role: str,
    status: str,
    workflow_run_id: str,
    step_run_id: str | None = None,
    work_order_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": card_id,
        "type": card_type,
        "title": title,
        "column": column,
        "owner_role": owner_role,
        "status": status,
        "workflow_run_id": workflow_run_id,
        "step_run_id": step_run_id,
        "work_order_id": work_order_id,
        "metadata": metadata or {},
    }


def build_kanban_projection(
    *,
    run: WorkflowRun,
    steps: list[StepRun],
    work_orders: list[WorkOrder],
    events: list[WorkflowEvent],
    board_name: str = "workflow",
) -> dict[str, Any]:
    """Build one internally consistent board view.

    Counts and card details are generated from the same card list, which avoids
    the historical mismatch where a column total said one thing and card detail
    said another.
    """

    latest_work_order_events = _latest_event_by_work_order(events)
    cards = [
        _card(
            card_id=run.workflow_run_id,
            card_type="workflow_run",
            title=run.title,
            column=_run_column(run.status),
            owner_role=run.created_by,
            status=run.status.value,
            workflow_run_id=run.workflow_run_id,
            metadata={
                "workflow_definition_id": run.workflow_definition_id,
                "workflow_version": run.workflow_version,
                "description": run.description,
                **run.metadata,
            },
        )
    ]

    for step in steps:
        cards.append(
            _card(
                card_id=step.step_run_id,
                card_type="step_run",
                title=step.step_key,
                column=_step_column(step.status),
                owner_role=step.owner_role,
                status=step.status.value,
                workflow_run_id=run.workflow_run_id,
                step_run_id=step.step_run_id,
                metadata=step.metadata,
            )
        )

    for work_order in work_orders:
        latest_event = latest_work_order_events.get(work_order.work_order_id)
        metadata = dict(work_order.metadata)
        if latest_event is not None:
            metadata["latest_event"] = {
                "event_id": latest_event.event_id,
                "event_type": latest_event.event_type,
                "actor": latest_event.actor,
                "created_at": latest_event.created_at.isoformat(),
            }
        cards.append(
            _card(
                card_id=work_order.work_order_id,
                card_type="work_order",
                title=work_order.task,
                column=_work_order_column(work_order.status),
                owner_role=work_order.owner_role,
                status=work_order.status.value,
                workflow_run_id=run.workflow_run_id,
                step_run_id=work_order.step_run_id,
                work_order_id=work_order.work_order_id,
                metadata={
                    **metadata,
                    "required_outputs": work_order.required_outputs,
                    "timeout_seconds": work_order.timeout_seconds,
                },
            )
        )

    counts = Counter(card["column"] for card in cards)
    columns = [
        {
            "id": column,
            "title": column.replace("_", " ").title(),
            "count": counts.get(column, 0),
            "cards": [card for card in cards if card["column"] == column],
        }
        for column in DEFAULT_COLUMNS
    ]

    return {
        "schema": KANBAN_SCHEMA,
        "board_name": board_name,
        "workflow_run_id": run.workflow_run_id,
        "workflow_definition_id": run.workflow_definition_id,
        "workflow_version": run.workflow_version,
        "title": run.title,
        "status": run.status.value,
        "summary": {
            "card_count": len(cards),
            "counts": {column: counts.get(column, 0) for column in DEFAULT_COLUMNS},
            "event_count": len(events),
        },
        "columns": columns,
        "cards": cards,
        "source_of_truth": "Hermes Orchestration Core",
    }

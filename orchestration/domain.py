"""Domain objects for Hermes durable orchestration.

The domain layer is intentionally storage-agnostic. PostgreSQL, in-memory tests,
and future queue adapters all speak through the same entities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


JsonDict = dict[str, Any]


class WorkflowRunStatus(StrEnum):
    ACTIVE = "active"
    WAITING_GATE = "waiting_gate"
    HOLD = "hold"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_GATE = "waiting_gate"
    HOLD = "hold"
    CHANGES_REQUESTED = "changes_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkOrderStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    HOLD = "hold"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class GateDecision(StrEnum):
    APPROVE = "approve"
    HOLD = "hold"
    CHANGES_REQUESTED = "changes_requested"
    REJECT = "reject"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass(slots=True)
class WorkflowRun:
    workflow_run_id: str
    workflow_definition_id: str
    workflow_version: str
    title: str
    status: WorkflowRunStatus
    current_step_id: str | None
    created_by: str
    metadata: JsonDict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class StepRun:
    step_run_id: str
    workflow_run_id: str
    step_key: str
    owner_role: str
    status: StepStatus
    metadata: JsonDict = field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkOrder:
    work_order_id: str
    workflow_run_id: str
    step_run_id: str
    owner_role: str
    task: str
    status: WorkOrderStatus
    required_outputs: list[str] = field(default_factory=list)
    inputs: JsonDict = field(default_factory=dict)
    timeout_seconds: int = 1800
    metadata: JsonDict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkflowEvent:
    event_id: str
    workflow_run_id: str
    event_type: str
    actor: str
    payload: JsonDict = field(default_factory=dict)
    step_run_id: str | None = None
    work_order_id: str | None = None
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class GateReview:
    gate_review_id: str
    workflow_run_id: str
    step_run_id: str
    requested_by: str
    reviewer_role: str
    status: GateStatus
    evidence_refs: list[str] = field(default_factory=list)
    notes: str = ""
    decision_by: str | None = None
    decision_notes: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    decided_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ArtifactRef:
    artifact_ref_id: str
    workflow_run_id: str
    artifact_type: str
    uri: str
    produced_by: str
    step_run_id: str | None = None
    work_order_id: str | None = None
    sha256: str | None = None
    metadata: JsonDict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AgentObservabilityLog:
    log_id: str
    workflow_run_id: str
    agent_id: str
    uri: str
    step_run_id: str | None = None
    work_order_id: str | None = None
    iteration: int = 1
    summary: str = ""
    sha256: str | None = None
    metadata: JsonDict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkOrderCallback:
    work_order_id: str
    attempt_id: str
    status: WorkOrderStatus
    actor: str
    commit_sha: str | None = None
    changed_files: list[str] = field(default_factory=list)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    agent_logs: list[AgentObservabilityLog] = field(default_factory=list)
    test_results: list[JsonDict] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    metrics: JsonDict = field(default_factory=dict)
    notes: str = ""

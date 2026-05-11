"""Built-in workflow packs for Hermes Orchestration Core."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkflowStage:
    key: str
    owner_role: str
    title: str
    required_outputs: tuple[str, ...] = ()
    timeout_seconds: int = 1800
    gate_reviewer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_outputs"] = list(self.required_outputs)
        return data


@dataclass(frozen=True, slots=True)
class WorkflowPack:
    workflow_definition_id: str
    version: str
    domain: str
    display_name: str
    description: str
    initial_step_key: str
    initial_owner_role: str
    methodology: str
    stages: tuple[WorkflowStage, ...]
    kanban_columns: tuple[str, ...] = (
        "backlog",
        "ready",
        "running",
        "review",
        "hold",
        "blocked",
        "done",
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_definition_json(self) -> dict[str, Any]:
        return {
            "workflow_definition_id": self.workflow_definition_id,
            "version": self.version,
            "domain": self.domain,
            "display_name": self.display_name,
            "description": self.description,
            "methodology": self.methodology,
            "initial_step_key": self.initial_step_key,
            "initial_owner_role": self.initial_owner_role,
            "stages": [stage.to_dict() for stage in self.stages],
            "kanban_columns": list(self.kanban_columns),
            "metadata": self.metadata,
        }

    def stage(self, key: str) -> WorkflowStage | None:
        normalized = key.strip().upper()
        for stage in self.stages:
            if stage.key == normalized:
                return stage
        return None


FACTORY_SCRUM_PROJECT = WorkflowPack(
    workflow_definition_id="factory.scrum_project",
    version="1.0.0",
    domain="software-factory",
    display_name="Factory Scrum Project",
    description="End-to-end Scrum workflow for Factory projects with micro-sprints and Zeus acceptance.",
    methodology="scrum.micro_sprints",
    initial_step_key="PROJECT_PLANNING",
    initial_owner_role="ana-pmo",
    stages=(
        WorkflowStage("PROJECT_PLANNING", "ana-pmo", "Project planning", ("planpack",), 1800),
        WorkflowStage("BACKLOG_READY", "ana-pmo", "Backlog ready", ("sprint_backlog",), 1800),
        WorkflowStage("SPRINT_PLANNING", "leo-orquestador", "Sprint planning", ("sprint_goal",), 1800),
        WorkflowStage("SPRINT_KICKOFF", "leo-orquestador", "Sprint kickoff", ("owner_map",), 900),
        WorkflowStage("SPRINT_EXECUTION", "factory-stage-owner", "Sprint execution", ("increment", "agent_log_ref"), 3600),
        WorkflowStage("SPRINT_REVIEW", "bruno-integrador", "Sprint review", ("review_notes",), 1800, "zeus"),
        WorkflowStage("QA_AND_REGATE", "tina-qa", "QA and re-gate", ("qa_report",), 1800, "zeus"),
        WorkflowStage("ZEUS_ACCEPTANCE", "zeus", "Zeus acceptance", ("decision",), 1800, "zeus"),
        WorkflowStage("SPRINT_RETROSPECTIVE", "ana-pmo", "Sprint retrospective", ("retro_notes",), 1200),
        WorkflowStage("NEXT_SPRINT_OR_RELEASE", "ana-pmo", "Next sprint or release", ("next_decision",), 900),
        WorkflowStage("RELEASE", "rene-release", "Release", ("release_notes",), 1800, "zeus"),
        WorkflowStage("MEMORY_UPDATE", "dario-docs", "Memory update", ("notion_report", "agent_logs_index"), 1200),
    ),
    metadata={
        "definition_of_ready": [
            "objective recorded",
            "acceptance criteria recorded",
            "sprint backlog selected",
        ],
        "definition_of_done": [
            "required work orders completed",
            "QA evidence linked",
            "Zeus acceptance recorded",
            "retrospective recorded",
        ],
    },
)


SOFTWARE_FAST_LANE = WorkflowPack(
    workflow_definition_id="software.simple_website.fast_lane",
    version="1.0.0",
    domain="software",
    display_name="Simple Website Fast Lane",
    description="Compact workflow for low-risk static sites and small UI surfaces.",
    methodology="fast_lane",
    initial_step_key="PLAN_LIGHT",
    initial_owner_role="ana-pmo",
    stages=(
        WorkflowStage("PLAN_LIGHT", "ana-pmo", "Light plan", ("acceptance_criteria",), 900),
        WorkflowStage("BUILD", "ciro-codex", "Build", ("commit_sha", "preview_ref", "agent_log_ref"), 1800),
        WorkflowStage("BROWSER_QA", "belen-browser", "Browser QA", ("browser_qa_report",), 1200, "zeus"),
        WorkflowStage("RELEASE", "rene-release", "Release", ("preview_url", "release_notes"), 1200, "zeus"),
        WorkflowStage("ZEUS_ACCEPTANCE", "zeus", "Zeus acceptance", ("decision",), 900, "zeus"),
        WorkflowStage("MEMORY_UPDATE", "dario-docs", "Memory update", ("agent_logs_index",), 900),
    ),
)


SOFTWARE_MULTI_SPRINT = WorkflowPack(
    workflow_definition_id="software.product.multi_sprint",
    version="1.0.0",
    domain="software",
    display_name="Product Multi-Sprint",
    description="Complex software workflow for backend, apps, integrations, and maintained products.",
    methodology="scrum.multi_sprint",
    initial_step_key="INTAKE",
    initial_owner_role="zeus",
    stages=(
        WorkflowStage("INTAKE", "zeus", "Intake", ("objective",), 900),
        WorkflowStage("DISCOVERY", "vera-research", "Discovery", ("research_dossier",), 1800),
        WorkflowStage("PRODUCT_SHAPING", "mia-producto", "Product shaping", ("prd",), 1800, "zeus"),
        WorkflowStage("ARCHITECTURE_REVIEW", "nico-arquitecto", "Architecture review", ("architecture_brief",), 1800, "zeus"),
        WorkflowStage("SPRINT_PLANNING", "ana-pmo", "Sprint planning", ("sprint_plan",), 1800),
        WorkflowStage("EXECUTION_SPRINT", "factory-stage-owner", "Execution sprint", ("increment", "tests", "agent_log_ref"), 3600),
        WorkflowStage("CODE_REVIEW", "bruno-integrador", "Code review", ("code_review",), 1800),
        WorkflowStage("QA_VALIDATION", "tina-qa", "QA validation", ("qa_report",), 1800, "zeus"),
        WorkflowStage("SECURITY_REVIEW", "sofia-secdevops", "Security review", ("security_review",), 1800, "zeus"),
        WorkflowStage("ZEUS_ACCEPTANCE", "zeus", "Zeus acceptance", ("decision",), 1800, "zeus"),
        WorkflowStage("RELEASE", "rene-release", "Release", ("release_notes",), 1800, "zeus"),
        WorkflowStage("RETROSPECTIVE", "ana-pmo", "Retrospective", ("retro_notes",), 1200),
        WorkflowStage("MEMORY_UPDATE", "dario-docs", "Memory update", ("notion_report",), 1200),
    ),
)


CONTENT_SOCIAL_CAMPAIGN = WorkflowPack(
    workflow_definition_id="content.social_campaign",
    version="1.0.0",
    domain="content",
    display_name="Social Campaign",
    description="Workflow for social media planning, content generation, approval, scheduling, and retrospective.",
    methodology="content_pipeline",
    initial_step_key="CAMPAIGN_BRIEF",
    initial_owner_role="content-strategist",
    stages=(
        WorkflowStage("CAMPAIGN_BRIEF", "content-strategist", "Campaign brief", ("brief",), 1200),
        WorkflowStage("CONTENT_BACKLOG", "ana-pmo", "Content backlog", ("content_calendar",), 1200),
        WorkflowStage("COPY_AND_ASSETS", "content-writer", "Copy and assets", ("draft_posts", "asset_refs", "agent_log_ref"), 1800),
        WorkflowStage("EDITORIAL_REVIEW", "editor", "Editorial review", ("editorial_notes",), 1200, "zeus"),
        WorkflowStage("SCHEDULING", "social-ops", "Scheduling", ("schedule_refs",), 1200),
        WorkflowStage("PUBLISH_REVIEW", "zeus", "Publish review", ("decision",), 900, "zeus"),
        WorkflowStage("RETROSPECTIVE", "ana-pmo", "Retrospective", ("campaign_retro",), 900),
    ),
)


BUILTIN_WORKFLOW_PACKS: dict[tuple[str, str], WorkflowPack] = {
    (pack.workflow_definition_id, pack.version): pack
    for pack in (
        FACTORY_SCRUM_PROJECT,
        SOFTWARE_FAST_LANE,
        SOFTWARE_MULTI_SPRINT,
        CONTENT_SOCIAL_CAMPAIGN,
    )
}


def get_workflow_pack(workflow_definition_id: str, version: str = "1.0.0") -> WorkflowPack | None:
    return BUILTIN_WORKFLOW_PACKS.get((workflow_definition_id, version))


def list_workflow_packs() -> list[WorkflowPack]:
    return list(BUILTIN_WORKFLOW_PACKS.values())


def workflow_pack_json() -> list[dict[str, Any]]:
    return [pack.to_definition_json() for pack in list_workflow_packs()]


---
name: factory-workflow
description: Zeus operating rules for canonical Factory workflows through Hermes Orchestration Core; use this instead of branch-Kanban-as-state playbooks.
category: devops
tags: [factory, openclaw, orchestration, scrum, gate-management]
version: 0.2.0
---

# Factory Workflow

## Canonical Rule

Hermes Orchestration Core is the execution state machine. Branch Kanban, Zeus Kanban, Telegram, Markdown logs, and Notion are views or evidence layers.

Do not change execution state by hand-editing a Kanban card or asking an agent repeatedly in chat. Use workflow APIs and MCP tools.

## Required Tools

- `openclaw_factory_project_request`: create a governed Factory Scrum workflow and delegate to Leo.
- `openclaw_orchestration_status`: inspect workflow run, steps, work orders, timeline, and Kanban projection.
- `openclaw_orchestration_kanban`: read the derived board for visibility.
- `openclaw_orchestration_watchdog`: detect stale work orders, mark timeouts, and trigger Zeus intervention.
- `openclaw_orchestration_intervention`: record executive action when a workflow is blocked or drifting.
- `openclaw_delegation_status`: inspect the transport-level delegation job.
- `openclaw_branch_report`: inspect office health and local evidence only.

## Workflow Pattern

1. Curate Jean's request into a clear objective and acceptance criteria.
2. Call `openclaw_factory_project_request` for software/product work.
3. Capture `workflow_run_id`, `sprint_id`, delegation task ID, and work order IDs.
4. Monitor with orchestration status and watchdog.
5. If a worker is silent or timed out, record intervention and re-slice or reroute from the same workflow.
6. Use Notion and Zeus Kanban for human reporting, never as runtime authority.
7. Accept only after QA, Browser QA, security/release gates, artifacts, and observability logs match the work orders.

## Timeout Pattern

If a delegation or worker stalls:

1. Run `openclaw_orchestration_watchdog`.
2. Inspect the blocked workflow with `openclaw_orchestration_status`.
3. Inspect office health with `openclaw_office_status` and `openclaw_branch_report`.
4. Record `openclaw_orchestration_intervention` with the decision:
   - inspect logs;
   - re-slice the task;
   - reroute owner/engine;
   - escalate to Jean;
   - cancel with notes.
5. Do not create a new blind fallback workflow unless Zeus explicitly closes or supersedes the current one.

## Evidence Contract

Every completed work order must have:

- owner role;
- artifact refs;
- agent observability log refs;
- commit SHA when code changed;
- QA/test results when applicable;
- gate decision where required;
- blockers and fallback attempts if any occurred.


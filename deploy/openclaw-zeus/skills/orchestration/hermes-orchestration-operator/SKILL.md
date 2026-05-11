---
name: hermes-orchestration-operator
description: Use this skill when Zeus must create, inspect, supervise, unblock, or close durable workflows through Hermes Orchestration Core.
version: 0.1.0
---

# Hermes Orchestration Operator

## Purpose

Operate Hermes Orchestration Core as the canonical execution control plane for multi-agent work.

Use this skill for Factory projects, cross-office workflows, repeated timeout/fallback failures, complex work that needs sprint increments, and any task where Markdown, Telegram, or a Kanban card would otherwise become accidental state.

## Source Of Truth

- Hermes Orchestration Core stores workflow runs, step runs, work orders, gates, callbacks, interventions, artifacts, agent logs, and events.
- Kanban boards are projections for human visibility. Do not hand-edit a Kanban card to change execution state.
- Agent Markdown logs are observability evidence. They explain reasoning and outputs; they do not drive the workflow.
- Notion is Jean's readable report layer. It links evidence and decisions; it is not the runtime engine.

## Zeus Workflow

1. Classify the work:
   - `software.simple_website.fast_lane` for narrow web tasks.
   - `factory.scrum_project` for governed Factory delivery.
   - `software.product.multi_sprint` for backend, product, or multi-sprint builds.
   - `content.social_campaign` for content production flows.
2. Create the workflow before delegating operational work.
3. Delegate with `workflow_run_id`, `sprint_id`, and assigned `work_order_id` in the task metadata.
4. Inspect status with `openclaw_orchestration_status`, not by asking repeatedly in chat.
5. Run `openclaw_orchestration_watchdog` when a task is stale, silent, or past its heartbeat window.
6. Record Zeus intervention with `openclaw_orchestration_intervention` when a timeout, repeated fallback, blocked gate, or drift needs executive action.
7. Mirror only high-level progress to Zeus Kanban and Notion.

## Supervision Rules

- A worker timeout marks the work order `timed_out`, moves the step to `hold`, blocks the run, and emits `zeus.intervention_required`.
- Repeated fallback is a signal to re-slice the work, change engine, or escalate the blocker; do not create another blind delegation with no state transition.
- A gate is closed only through gate decision APIs and evidence references.
- A sprint closes only after unfinished work orders are completed, cancelled, or explicitly force-closed with notes.

## Factory Scrum Rules

- Zeus creates the canonical workflow; Leo Orquestador owns operational sprint routing.
- Ana PMO owns sprint opening/closeout evidence and retrospective capture.
- Specialist agents own their work orders and must return artifact refs plus agent observability logs.
- Zeus accepts or rejects meaningful increments after QA/security/release evidence is attached.

## Required Evidence

- Workflow run ID.
- Sprint ID when applicable.
- Work order IDs and owners.
- Artifact refs.
- Agent observability log refs.
- Gate decisions.
- Timeout/intervention events when supervision happened.
- Final review and retrospective notes.


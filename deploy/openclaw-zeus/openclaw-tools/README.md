# Hermes OpenClaw Tools

MCP server for Hermes Agent on `hermes-agent-01`.

It gives Hermes a small, explicit control surface for SitioUno/OpenClaw offices:

- `openclaw_identity`
- `openclaw_list_offices`
- `openclaw_office_status`
- `openclaw_tailnet_status`
- `openclaw_orchestration_workflow_definitions`
- `openclaw_orchestration_start_factory_project`
- `openclaw_orchestration_status`
- `openclaw_orchestration_kanban`
- `openclaw_orchestration_watchdog`
- `openclaw_orchestration_intervention`
- `openclaw_factory_project_request`
- `openclaw_delegate_task`
- `openclaw_delegation_runbook`

Secrets are not stored in `openclaw-fleet.yaml`. Delegation tokens must be set
in `~/.hermes/.env`, for example:

```bash
OPENCLAW_SICILIA_DELEGATE_TOKEN=...
OPENCLAW_MIAMI_DELEGATE_TOKEN=...
HERMES_ORCHESTRATION_API_KEY=...
HERMES_ORCHESTRATION_API_URL=http://127.0.0.1:8650
```

Hermes config uses an MCP server named `openclaw-office`.

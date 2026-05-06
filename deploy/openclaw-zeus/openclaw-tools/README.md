# Hermes OpenClaw Tools

MCP server for Hermes Agent on `hermes-agent-01`.

It gives Hermes a small, explicit control surface for SitioUno/OpenClaw offices:

- `openclaw_identity`
- `openclaw_list_offices`
- `openclaw_office_status`
- `openclaw_tailnet_status`
- `openclaw_delegate_task`
- `openclaw_delegation_runbook`

Secrets are not stored in `openclaw-fleet.yaml`. Delegation tokens must be set
in `~/.hermes/.env`, for example:

```bash
OPENCLAW_SICILIA_DELEGATE_TOKEN=...
OPENCLAW_MIAMI_DELEGATE_TOKEN=...
```

Hermes config uses an MCP server named `openclaw-office`.

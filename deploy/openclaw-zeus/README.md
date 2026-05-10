# OpenClaw Zeus Deployment Assets

This directory contains the reproducible, non-secret assets used for the Sitio
Uno GCP Zeus deployment.

Copy these files into `/home/hermes/.hermes` on a Hermes VM after the base Hermes
installation is complete.

## Layout

- `SOUL.md` - Zeus identity/persona.
- `docs/ZEUS-FACTORY-OPERATING-MODEL.md` - Zeus operating model for strategic delegation to the Sitio Uno Software Factory.
- `honcho.json` - Honcho memory configuration without API keys.
- `config/config.yaml.fragment.example` - config fragment for memory and MCP servers.
- `openclaw-tools/` - OpenClaw fleet MCP and MiroFish MCP wrappers.
- `bin/start-browser-cdp.sh` - local-only Chrome CDP launcher for advanced browser tools.
- `honcho-backup/backup_honcho.py` - local Honcho workspace exporter.
- `systemd/user/` - user service/timer templates.
- `systemd/user/hermes-orchestration.service` - local-only durable orchestration API.
- `skills/research/llm-wiki/` - local LLM wiki skill reference.

## Install Sketch

```bash
sudo -iu hermes
install -d ~/.hermes/bin ~/.hermes/openclaw-tools ~/.hermes/honcho-backup ~/.config/systemd/user
cp SOUL.md ~/.hermes/SOUL.md
cp honcho.json ~/.hermes/honcho.json
cp bin/start-browser-cdp.sh ~/.hermes/bin/
chmod 0755 ~/.hermes/bin/start-browser-cdp.sh
cp openclaw-tools/* ~/.hermes/openclaw-tools/
cp honcho-backup/backup_honcho.py ~/.hermes/honcho-backup/
cp systemd/user/honcho-backup.* ~/.config/systemd/user/
cp systemd/user/hermes-browser-cdp.service ~/.config/systemd/user/
cp systemd/user/hermes-gateway.service ~/.config/systemd/user/
cp systemd/user/hermes-orchestration.service ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/hermes-gateway.service.d
cp systemd/user/hermes-gateway.service.d/*.conf ~/.config/systemd/user/hermes-gateway.service.d/
```

Merge `config/config.yaml.fragment.example` into `~/.hermes/config.yaml`.

Set general Hermes secrets only in `~/.hermes/.env`, for example:

```bash
HONCHO_API_KEY=...
OPENCLAW_SICILIA_DELEGATE_TOKEN=...
MIROFISH_ENABLE_EXPENSIVE_TOOLS=1
```

Do not store the Factory SendGrid dev API key in Hermes files or `.env`.
Zeus should only reference the Google Secret Manager secret id
`factory-sendgrid-dev-api-key` in project `su-office-2030`; the Factory runner
reads it through approved service-account access when an email/OTP E2E test
requires it.

Set Orchestration Core secrets in `~/.hermes/orchestration.env`, sourced from
Google Secret Manager by the deploy process:

```bash
HERMES_ORCHESTRATION_DATABASE_URL=...
HERMES_ORCHESTRATION_API_KEY=...
HERMES_ORCHESTRATION_NODE_ID=zeus
```

Keep this file mode `0600`.

Zeus should use `openclaw_branch_report(<office_id>)` to read each branch
`GET /v1/delegate` report. The report's `kanban` block feeds Zeus' Hermes
Kanban for strategic oversight while the branch keeps its own local operational
board.

For development work, Zeus should use `openclaw_factory_project_request` instead
of raw `openclaw_delegate_task`. The Factory project tool writes the Sicilia
Kanban project and canonical stage cards before it delegates async to Leo, so
web/app/repo/preview work cannot disappear into an untracked agent turn.

Then reload user systemd:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-browser-cdp.service
systemctl --user enable --now hermes-gateway.service
systemctl --user enable --now hermes-orchestration.service
systemctl --user enable --now honcho-backup.timer
```

## Validate

```bash
hermes doctor --fix
hermes memory status
curl -fsS http://127.0.0.1:9222/json/version
curl -fsS http://127.0.0.1:8650/health
systemctl --user status hermes-browser-cdp.service
systemctl --user status hermes-gateway.service
systemctl --user status hermes-orchestration.service
systemctl --user status honcho-backup.timer
```

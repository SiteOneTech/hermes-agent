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
- `honcho-backup/backup_honcho.py` - local Honcho workspace exporter.
- `systemd/user/` - user service/timer templates.
- `skills/research/llm-wiki/` - local LLM wiki skill reference.

## Install Sketch

```bash
sudo -iu hermes
install -d ~/.hermes/openclaw-tools ~/.hermes/honcho-backup ~/.config/systemd/user
cp SOUL.md ~/.hermes/SOUL.md
cp honcho.json ~/.hermes/honcho.json
cp openclaw-tools/* ~/.hermes/openclaw-tools/
cp honcho-backup/backup_honcho.py ~/.hermes/honcho-backup/
cp systemd/user/honcho-backup.* ~/.config/systemd/user/
cp systemd/user/hermes-gateway.service ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/hermes-gateway.service.d
cp systemd/user/hermes-gateway.service.d/*.conf ~/.config/systemd/user/hermes-gateway.service.d/
```

Merge `config/config.yaml.fragment.example` into `~/.hermes/config.yaml`.

Set secrets only in `~/.hermes/.env`, for example:

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

Zeus should use `openclaw_branch_report(<office_id>)` to read each branch
`GET /v1/delegate` report. The report's `kanban` block feeds Zeus' Hermes
Kanban for strategic oversight while the branch keeps its own local operational
board.

Then reload user systemd:

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway.service
systemctl --user enable --now honcho-backup.timer
```

## Validate

```bash
hermes doctor --fix
hermes memory status
systemctl --user status hermes-gateway.service
systemctl --user status honcho-backup.timer
```

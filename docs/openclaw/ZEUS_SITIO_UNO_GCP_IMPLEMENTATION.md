# Zeus - Sitio Uno GCP Hermes Implementation

Date: 2026-05-06

This document records the OpenClaw/Sitio Uno production implementation of Hermes as
the Zeus agent inside the `su-office-2030` GCP office.

## Runtime Identity

Zeus is a Hermes deployment, but the operator-facing identity is **Zeus**:

> Soy Zeus, tu asistente IA dentro de la infraestructura de Sitio Uno GCP.
> Trabajo para Jean Garcia, CEO de Sitio Uno.

The active persona is stored in:

- `~/.hermes/SOUL.md`
- repository template: `deploy/openclaw-zeus/SOUL.md`

Important corrections applied:

- Zeus must not introduce itself as Hermes unless Jean asks about the platform.
- Zeus must not describe the main company as "OpenClaw GCP"; Sitio Uno is the
  company/ecosystem, OpenClaw is the operational office/agent network.
- Legacy persistent memory was cleared so old Hermes/OpenClaw wording would not
  override the new identity.

## Infrastructure

Production VM:

- GCP project: `su-office-2030`
- Zone: `us-central1-a`
- VM: `hermes-agent-01`
- Runtime user: `hermes`
- Hermes home: `/home/hermes/.hermes`
- Tailscale address observed during setup: `100.90.65.123`
- Hermes UI/gateway port used during setup: `9119`

The gateway runs as a **user systemd service**, not a root/global service:

```bash
uid=$(id -u hermes)
sudo -u hermes env HOME=/home/hermes XDG_RUNTIME_DIR=/run/user/$uid \
  systemctl --user status hermes-gateway.service
```

This is the correct fix for the UI error:

```text
System gateway restart requires root. Re-run with sudo.
```

The service file and restart drop-in are captured under:

- `deploy/openclaw-zeus/systemd/user/hermes-gateway.service`
- `deploy/openclaw-zeus/systemd/user/hermes-gateway.service.d/restart-speed.conf`

## Providers And Tools

Validated by `hermes doctor --fix` on 2026-05-06:

- OpenAI Codex auth: logged in through account auth, not API key billing.
- Anthropic API: reachable.
- DeepSeek API: reachable.
- MiniMax API: reachable.
- Docker daemon: running.
- Node.js: installed.
- `agent-browser`: installed.
- Honcho memory provider: connected.
- Telegram gateway: configured in polling mode; no public inbound port required.

Known remaining optional items:

- `GOOGLE_API_KEY` is invalid or not intended for active use.
- OpenRouter is not configured.
- Discord/Home Assistant/MOA/RL/Yuanbao/Spotify tools remain optional and gated
  by missing credentials or system dependencies.
- `GITHUB_TOKEN` is recommended in `~/.hermes/.env` to avoid GitHub rate limits
  for skills hub operations.

## Honcho Memory

Honcho was enabled as the external memory provider.

Runtime files:

- `~/.hermes/honcho.json`
- `~/.hermes/config.yaml` with `memory.provider: honcho`

Repository templates:

- `deploy/openclaw-zeus/honcho.json`
- `deploy/openclaw-zeus/config/config.yaml.fragment.example`

Configuration summary:

- Workspace: `sitio-uno-gcp`
- Human peer: `jean`
- AI peer: `zeus`
- Recall mode: `hybrid`
- Write frequency: `async`
- Session strategy: `per-session`

The Honcho API key is **not** stored in `honcho.json`. It must remain in
`~/.hermes/.env` as `HONCHO_API_KEY`.

Local backup was added:

- Script: `~/.hermes/honcho-backup/backup_honcho.py`
- Timer: `~/.config/systemd/user/honcho-backup.timer`
- Backup directory: `~/.hermes/honcho-backups/`
- Retention: 30 backup runs

Repository templates:

- `deploy/openclaw-zeus/honcho-backup/backup_honcho.py`
- `deploy/openclaw-zeus/systemd/user/honcho-backup.service`
- `deploy/openclaw-zeus/systemd/user/honcho-backup.timer`

## OpenClaw Office Delegation

The OpenClaw fleet MCP server gives Zeus a registry of offices and agents. Network
reachability through Tailscale is not treated as authority. Delegation requires:

1. Office registration in `openclaw-fleet.yaml`.
2. A delegate endpoint per office.
3. A per-office token in the local environment.
4. A dry-run check before live execution.

Runtime files:

- `~/.hermes/openclaw-tools/openclaw_mcp_server.py`
- `~/.hermes/openclaw-tools/openclaw-fleet.yaml`

Repository templates:

- `deploy/openclaw-zeus/openclaw-tools/openclaw_mcp_server.py`
- `deploy/openclaw-zeus/openclaw-tools/openclaw-fleet.yaml`

Tokens are referenced by environment variable names such as:

- `OPENCLAW_SICILIA_DELEGATE_TOKEN`
- `OPENCLAW_MIAMI_DELEGATE_TOKEN`

Token values are never stored in the fleet YAML.

## MiroFish Simulator Integration

Zeus was given an MCP wrapper for the MiroFish simulator.

Runtime files:

- `~/.hermes/openclaw-tools/mirofish_mcp_server.py`

Repository template:

- `deploy/openclaw-zeus/openclaw-tools/mirofish_mcp_server.py`

The active simulator endpoint observed during setup:

- VM: `mirofish-simulator-01`
- Private IP: `10.42.0.4`
- Tailscale IP: `100.119.34.35`
- Production UI: `http://100.119.34.35/`
- Graphiti staging UI: `http://100.119.34.35:8082/`

Operational policy added to Zeus:

- Zeus may use MiroFish only when Jean explicitly asks for simulation, report, or
  simulator inspection.
- Zeus must check existing projects/simulations/reports before launching costly
  work.
- Zeus must distinguish observed facts, simulator inference, and Zeus
  recommendations.
- Tools that can spend LLM tokens or mutate simulator state are gated by
  `MIROFISH_ENABLE_EXPENSIVE_TOOLS`.

## Skill Additions

The Sitio Uno LLM wiki reference was added to the local Hermes skills directory:

- `~/.hermes/skills/research/llm-wiki/references/sitiouno-llm-wiki.md`

Repository copy:

- `deploy/openclaw-zeus/skills/research/llm-wiki/references/sitiouno-llm-wiki.md`

## Verification Commands

Run as the `hermes` user:

```bash
sudo -u hermes env HOME=/home/hermes hermes doctor --fix
sudo -u hermes env HOME=/home/hermes hermes memory status
```

Check gateway:

```bash
uid=$(id -u hermes)
sudo -u hermes env HOME=/home/hermes XDG_RUNTIME_DIR=/run/user/$uid \
  systemctl --user status hermes-gateway.service
```

Check Honcho backup timer:

```bash
uid=$(id -u hermes)
sudo -u hermes env HOME=/home/hermes XDG_RUNTIME_DIR=/run/user/$uid \
  systemctl --user status honcho-backup.timer
```

Check MCP children:

```bash
pgrep -af 'openclaw_mcp_server|mirofish_mcp_server'
```

## Security Notes

- Do not commit `~/.hermes/.env`.
- Do not commit state databases, sessions, logs, memory files, or backup payloads.
- Do not print API keys in diagnostics.
- Keep GitHub access as a restricted token or dedicated service/user account with
  repo-scoped permissions.
- For company repos, enforce branch protection and PR requirements on `main`
  rather than relying on agent behavior alone.


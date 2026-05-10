# Hermes Orchestration Core

## Document Control

**Version:** 0.1.0
**Date:** 2026-05-10
**Owner:** Hermes Orchestration Core
**Status:** Active implementation note

## Version History

| Version | Date | Author | Notes |
| --- | --- | --- | --- |
| 0.1.0 | 2026-05-10 | Codex | Documents the first Postgres-backed orchestration runtime surface and Cloud SQL deployment contract. |

## Purpose

Hermes Orchestration Core is the durable control plane for Factory and future
branch workflows. It stores machine-readable workflow state in Postgres and
keeps Markdown agent logs as observability evidence for Jean and Notion.

## Runtime Ownership

| Concern | Owner |
| --- | --- |
| Runtime package, API, migrations | `hermes-agent` |
| Workflow definitions and Factory contracts | `sitiouno-software-factory-ai` |
| Cloud SQL, VPC, Secret Manager, service accounts | `gcloud-office` / GCP project `su-office-2030` |

## Environment

Required runtime variables:

```bash
HERMES_ORCHESTRATION_DATABASE_URL=postgresql://hermes_orchestrator:PASSWORD@PRIVATE_IP:5432/hermes_orchestration
HERMES_ORCHESTRATION_API_KEY=...
HERMES_ORCHESTRATION_NODE_ID=zeus
```

Store production values in Secret Manager or a protected environment file. Do
not commit live values.

## API Surface

The dedicated `hermes-orchestration` server exposes internal orchestration
routes under `/v1`. The Hermes dashboard also mounts the same router for local
operator use. All routes require
`Authorization: Bearer $HERMES_ORCHESTRATION_API_KEY`.

Initial routes:

```text
POST /v1/workflow-runs
GET  /v1/workflow-runs/{workflow_run_id}
GET  /v1/workflow-runs/{workflow_run_id}/timeline
POST /v1/workflow-runs/{workflow_run_id}/work-orders
POST /v1/work-orders/{work_order_id}/callback
POST /v1/gates/{gate_review_id}/decision
POST /v1/webhooks/factory-event
```

## Migration Command

```bash
python -m orchestration.migrate --database-url "$HERMES_ORCHESTRATION_DATABASE_URL"
```

The migration runner records applied files in `schema_migrations` and is safe
to re-run.

## Systemd Service

Zeus deployments use:

```text
deploy/openclaw-zeus/systemd/user/hermes-orchestration.service
```

Default bind:

```text
127.0.0.1:8650
```

Expose it to other branches only through an approved tunnel, private reverse
proxy, or service-to-service route.

## Control Plane Rule

Structured tables and events drive workflow state. Agent Markdown logs are
linked through `agent_observability_logs` and `artifact_refs`, but they do not
advance gates by themselves.

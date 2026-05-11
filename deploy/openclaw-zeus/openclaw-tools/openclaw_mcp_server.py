#!/usr/bin/env python3
"""MCP tools that let Hermes understand and delegate to OpenClaw offices.

The server is intentionally small and conservative:
- Inventory lives in a YAML file with no secrets.
- Delegation tokens are read from environment variables only.
- The remote contract is the documented branch-delegation-v1 HTTP API.
"""

from __future__ import annotations

import argparse
import base64
import smtplib
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin

try:
    import yaml
except Exception:  # pragma: no cover - Hermes ships PyYAML, fallback is for diagnostics.
    yaml = None

from mcp.server.fastmcp import FastMCP


DEFAULT_CONFIG = Path.home() / ".hermes" / "openclaw-tools" / "openclaw-fleet.yaml"
DEFAULT_NOTION_STATE = Path.home() / ".hermes" / "openclaw-tools" / "notion-state.json"
DEFAULT_ZEUS_KANBAN_DB = Path.home() / ".hermes" / "kanban.db"
MAX_DEADLINE_S = 600
DEFAULT_TIMEOUT_S = 8
DEFAULT_REGISTRY_API_URL = "http://openclaw-hq:8781"
DEFAULT_ORCHESTRATION_API_URL = "http://127.0.0.1:8650"
DEFAULT_NOTION_VERSION = "2022-06-28"
FACTORY_CANONICAL_STAGES = (
    ("idea", "IDEA", "leo-orquestador", "done"),
    ("discovery", "DISCOVERY", "vera-research", "ready"),
    ("product-shaping", "PRODUCT_SHAPING", "mia-producto", "backlog"),
    ("architecture-review", "ARCHITECTURE_REVIEW", "nico-arquitecto", "backlog"),
    ("ready-for-sprint", "READY_FOR_SPRINT", "leo-orquestador", "backlog"),
    ("execution", "EXECUTION", "olga-openhands", "backlog"),
    ("code-review", "CODE_REVIEW", "bruno-integrador", "backlog"),
    ("qa-validation", "QA_VALIDATION", "tina-qa", "backlog"),
    ("security-review", "SECURITY_REVIEW", "sofia-secdevops", "backlog"),
    ("zeus-acceptance", "ZEUS_ACCEPTANCE", "leo-orquestador", "backlog"),
    ("release", "RELEASE", "rene-release", "backlog"),
    ("retrospective", "RETROSPECTIVE", "ana-pmo", "backlog"),
    ("memory-update", "MEMORY_UPDATE", "dario-docs", "backlog"),
)
_DOTENV_LOADED = False

mcp = FastMCP("openclaw-office")


def _config_path() -> Path:
    return Path(os.getenv("OPENCLAW_FLEET_CONFIG", str(DEFAULT_CONFIG))).expanduser()


def _ensure_dotenv_loaded() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    hermes_home = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    for dotenv in (hermes_home / ".env", hermes_home / "orchestration.env"):
        if not dotenv.exists():
            continue
        for raw_line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    _ensure_dotenv_loaded()
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if value.startswith("${") and value.endswith("}"):
        return default
    return value


def _slugify(value: str, fallback: str = "project") -> str:
    text = str(value or "").strip().lower()
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
    )
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return (text or fallback)[:64].strip("-") or fallback


def _collapse_repeated_numeric_suffix(value: str) -> str:
    collapsed = str(value or "").strip("-")
    while re.search(r"-(\d+)-\1$", collapsed):
        collapsed = re.sub(r"-(\d+)-\1$", r"-\1", collapsed)
    return collapsed


def _canonical_factory_slug(value: str, fallback: str = "factory-project") -> str:
    slug = _slugify(value, fallback=fallback)
    slug = _collapse_repeated_numeric_suffix(slug)
    for prefix in ("factory-su-", "factory-"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    slug = re.sub(r"-0*\d+$", "", slug).strip("-") or fallback
    # Known production alias: Jean used both forms while testing the same project.
    if "pagoda" in slug and "ccs" in slug:
        return "la-pagoda-ccs"
    return slug


def _canonical_factory_pid(project_id: str, slug: str) -> str:
    explicit = _slugify(project_id, fallback="")
    if not explicit:
        return f"factory-{slug}-001"
    explicit = _collapse_repeated_numeric_suffix(explicit)
    if explicit.startswith("factory-su-"):
        explicit = f"factory-{explicit[len('factory-su-') :]}"
    elif not explicit.startswith("factory-"):
        explicit = f"factory-{_canonical_factory_slug(explicit)}-001"
    if "pagoda" in explicit and "ccs" in explicit:
        return "factory-la-pagoda-ccs-001"
    return explicit


def _factory_project_id(title: str, project_slug: str = "", project_id: str = "") -> tuple[str, str, str]:
    slug_source = project_id or project_slug or title
    slug = _canonical_factory_slug(slug_source, fallback="factory-project")
    pid = _canonical_factory_pid(project_id, slug)
    repo_name = f"factory-su-{slug}"
    return pid, slug, repo_name


def _factory_backlog_items(pid: str, title: str, complexity: str) -> list[dict[str, Any]]:
    timeout_by_complexity = {
        "simple": 1800,
        "standard": 3600,
        "complex": 7200,
    }
    timeout_seconds = timeout_by_complexity.get(complexity, 3600)
    items: list[dict[str, Any]] = []
    for index, (stage_key, stage_name, owner, _status) in enumerate(FACTORY_CANONICAL_STAGES, start=1):
        items.append(
            {
                "backlog_item_id": f"{stage_key}-{index:02d}",
                "task": (
                    f"{stage_name}: execute the canonical Factory stage for {title}. "
                    f"Record owner evidence, artifact references and agent observability logs."
                ),
                "owner_role": owner,
                "required_outputs": [
                    f"{stage_key}_evidence",
                    "artifact_ref",
                    "agent_log_ref",
                    "gate_status",
                ],
                "inputs": {
                    "project_id": pid,
                    "stage": stage_name,
                    "stage_key": stage_key,
                },
                "timeout_seconds": timeout_seconds,
                "expected_first_heartbeat_seconds": min(timeout_seconds, 900),
                "retry_policy": {"max_attempts": 1},
            }
        )
    return items


def _factory_initial_delegation_work_order_id(work_orders: list[dict[str, Any]]) -> str:
    """Return the first concrete Core work order Zeus delegates to Sicilia."""
    for item in work_orders:
        if not isinstance(item, dict):
            continue
        inputs = item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        candidate = str(item.get("work_order_id") or "").strip()
        if (
            item.get("owner_role") == "leo-orquestador"
            and inputs.get("stage") == "IDEA"
            and _looks_like_work_order_id(candidate)
        ):
            return candidate
    for item in work_orders:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("work_order_id") or "").strip()
        if item.get("owner_role") == "leo-orquestador" and _looks_like_work_order_id(candidate):
            return candidate
    for item in work_orders:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("work_order_id") or "").strip()
        if _looks_like_work_order_id(candidate):
            return candidate
    return ""


def _github_api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    token = _env("GH_TOKEN") or _env("GITHUB_TOKEN")
    if not token:
        return {"ok": False, "error": "missing GH_TOKEN or GITHUB_TOKEN"}
    url = f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "hermes-openclaw-factory/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    started = time.monotonic()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read(262144).decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(text) if text else {}
            except json.JSONDecodeError:
                parsed = {"raw": text[:1024]}
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "body_json": parsed,
                "duration_s": round(time.monotonic() - started, 3),
            }
    except urllib.error.HTTPError as exc:
        text = exc.read(4096).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "error": text or str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }


def _ensure_factory_github_repo(repo_name: str, title: str, project_slug: str) -> dict[str, Any]:
    owner = "SiteOneTech"
    repo_name = _slugify(repo_name, fallback=f"factory-su-{project_slug}")
    repo = _github_api_request("GET", f"/repos/{owner}/{quote(repo_name)}")
    if repo.get("ok"):
        body = repo.get("body_json") if isinstance(repo.get("body_json"), dict) else {}
        ensure = _ensure_factory_repo_initialized(
            owner=owner,
            repo_name=repo_name,
            title=title,
            repo=body,
        )
        return {
            "ok": bool(ensure.get("ok")),
            "action": "existing",
            "repo_name": repo_name,
            "repo_url": body.get("html_url") or f"https://github.com/{owner}/{repo_name}",
            "clone_url": body.get("clone_url") or f"https://github.com/{owner}/{repo_name}.git",
            "visibility": body.get("visibility") or ("private" if body.get("private") else "unknown"),
            "default_branch": ensure.get("default_branch") or body.get("default_branch") or "",
            "initialized": ensure,
        }
    if repo.get("status") not in {403, 404}:
        return {
            "ok": False,
            "action": "lookup_failed",
            "repo_name": repo_name,
            "error": repo.get("error") or f"GitHub lookup failed with status {repo.get('status')}",
            "github": repo,
        }

    created = _github_api_request(
        "POST",
        f"/orgs/{owner}/repos",
        payload={
            "name": repo_name,
            "description": f"{title} managed by SitioUno Factory",
            "private": True,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
            "auto_init": True,
        },
        timeout_s=20.0,
    )
    if not created.get("ok"):
        return {
            "ok": False,
            "action": "create_failed",
            "repo_name": repo_name,
            "error": created.get("error") or f"GitHub create failed with status {created.get('status')}",
            "github": created,
        }
    body = created.get("body_json") if isinstance(created.get("body_json"), dict) else {}
    return {
        "ok": True,
        "action": "created",
        "repo_name": repo_name,
        "repo_url": body.get("html_url") or f"https://github.com/{owner}/{repo_name}",
        "clone_url": body.get("clone_url") or f"https://github.com/{owner}/{repo_name}.git",
        "visibility": body.get("visibility") or "private",
        "default_branch": body.get("default_branch") or "main",
        "initialized": {"ok": True, "action": "auto_init"},
    }


def _ensure_factory_repo_initialized(
    *,
    owner: str,
    repo_name: str,
    title: str,
    repo: dict[str, Any],
) -> dict[str, Any]:
    default_branch = str(repo.get("default_branch") or "main").strip() or "main"
    branch = _github_api_request(
        "GET",
        f"/repos/{owner}/{quote(repo_name)}/branches/{quote(default_branch)}",
    )
    if branch.get("ok"):
        return {"ok": True, "action": "already_initialized", "default_branch": default_branch}

    readme = (
        f"# {title}\n\n"
        "Managed by SitioUno Software Factory through Hermes Orchestration Core.\n"
    )
    initialized = _github_api_request(
        "PUT",
        f"/repos/{owner}/{quote(repo_name)}/contents/README.md",
        payload={
            "message": "chore: initialize factory repository",
            "content": base64.b64encode(readme.encode("utf-8")).decode("ascii"),
        },
        timeout_s=20.0,
    )
    if not initialized.get("ok"):
        return {
            "ok": False,
            "action": "initialize_failed",
            "error": initialized.get("error") or f"GitHub initialize failed with status {initialized.get('status')}",
            "github": initialized,
        }
    refreshed = _github_api_request("GET", f"/repos/{owner}/{quote(repo_name)}")
    refreshed_body = (
        refreshed.get("body_json")
        if isinstance(refreshed.get("body_json"), dict)
        else {}
    )
    return {
        "ok": True,
        "action": "initialized",
        "default_branch": refreshed_body.get("default_branch") or "main",
    }


def _zeus_kanban_upsert(
    *,
    task_id: str,
    title: str,
    body: str,
    status: str,
    priority: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    db_path = Path(_env("HERMES_KANBAN_DB", str(DEFAULT_ZEUS_KANBAN_DB))).expanduser()
    if not db_path.exists():
        return {"ok": False, "error": f"Zeus kanban db not found: {db_path}", "db_path": str(db_path)}
    now = int(time.time())
    payload = json.dumps(result, ensure_ascii=True, sort_keys=True)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                  id, title, body, assignee, status, priority, created_by,
                  created_at, workspace_kind, result, idempotency_key, tenant
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  title = excluded.title,
                  body = excluded.body,
                  assignee = excluded.assignee,
                  status = excluded.status,
                  priority = excluded.priority,
                  result = excluded.result
                """,
                (
                    task_id,
                    title[:240],
                    body[:4000],
                    "Zeus",
                    status,
                    int(priority),
                    "openclaw_factory_project_request",
                    now,
                    "scratch",
                    payload,
                    task_id,
                    "default",
                ),
            )
            conn.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) VALUES(?, ?, ?, ?, ?)",
                (task_id, None, "factory_project_tracking_updated", payload, now),
            )
            conn.commit()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "db_path": str(db_path)}
    return {"ok": True, "task_id": task_id, "status": status, "db_path": str(db_path)}


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {
            "site": {"id": "unknown", "display_name": "Unknown"},
            "offices": {},
            "error": f"fleet config not found: {path}",
        }
    text = path.read_text(encoding="utf-8")
    if yaml is None:
        return json.loads(text)
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"fleet config must be a mapping: {path}")
    data["_config_path"] = str(path)
    return data


def _registry_base_url(cfg: dict[str, Any]) -> str:
    site = cfg.get("site") or {}
    return str(
        _env("OPENCLAW_REGISTRY_API_URL")
        or site.get("registry_api_url")
        or DEFAULT_REGISTRY_API_URL
    ).rstrip("/")


def _registry_branches(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or _load_config()
    base_url = _registry_base_url(cfg)
    if not base_url:
        return {"ok": False, "error": "registry api url not configured", "branches": []}
    url = urljoin(f"{base_url}/", "v1/branches")
    started = time.monotonic()
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read(262144).decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            branches = data.get("branches") if isinstance(data, dict) else None
            if not isinstance(branches, list):
                return {
                    "ok": False,
                    "url": url,
                    "status": resp.status,
                    "error": "registry response missing branches[]",
                    "duration_s": round(time.monotonic() - started, 3),
                    "branches": [],
                }
            return {
                "ok": True,
                "url": url,
                "status": resp.status,
                "duration_s": round(time.monotonic() - started, 3),
                "branches": branches,
            }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
            "branches": [],
        }


def _registry_agents(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    for agent in row.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        agent_id = str(agent.get("agent_id") or agent.get("id") or agent.get("name") or "").strip()
        if not agent_id:
            continue
        item = {
            "role": agent.get("role"),
            "model": agent.get("model"),
            "updated_at": agent.get("updated_at"),
        }
        extra = agent.get("extra")
        if isinstance(extra, dict):
            item.update({f"extra_{k}": v for k, v in extra.items() if k not in item})
        agents[agent_id] = {k: v for k, v in item.items() if v is not None}
    return agents


def _office_from_registry(row: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    office = deepcopy(existing or {})
    branch_id = str(row.get("branch_id") or "").strip().lower()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    private_mesh = metadata.get("private_mesh") if isinstance(metadata.get("private_mesh"), dict) else {}
    office.setdefault("kind", "branch-office" if branch_id != "hq" else "gcp-office")
    office["status"] = "active" if row.get("online_hint") else office.get("status", "registered")
    office["display_name"] = row.get("display_name") or office.get("display_name") or branch_id
    office["node_id"] = row.get("node_id") or office.get("node_id")
    if private_mesh.get("node_ip"):
        office["tailscale_ip"] = private_mesh.get("node_ip")
    if row.get("delegate_url") and not office.get("delegate_endpoint"):
        office["delegate_endpoint"] = row.get("delegate_url")
    if private_mesh.get("node_ip") and not office.get("delegate_endpoint"):
        office["delegate_endpoint"] = f"http://{private_mesh['node_ip']}:8780/v1/delegate"
    if branch_id and branch_id != "hq" and not office.get("token_env"):
        office["token_env"] = f"OPENCLAW_{branch_id.upper()}_DELEGATE_TOKEN"
    office.setdefault("contract", "branch-delegation-v1")

    registry_agents = _registry_agents(row)
    if registry_agents:
        office["agents"] = registry_agents
    office["registry"] = {
        "source": "hq-sidecar",
        "online_hint": row.get("online_hint"),
        "last_heartbeat": row.get("last_heartbeat"),
        "updated_at": row.get("updated_at"),
        "metadata": metadata,
    }
    return office


def _merged_config() -> dict[str, Any]:
    cfg = _load_config()
    offices = deepcopy(cfg.get("offices") or {})
    if not isinstance(offices, dict):
        offices = {}
    registry = _registry_branches(cfg)
    for row in registry.get("branches") or []:
        if not isinstance(row, dict):
            continue
        branch_id = str(row.get("branch_id") or "").strip().lower()
        if not branch_id:
            continue
        offices[branch_id] = _office_from_registry(row, offices.get(branch_id))
    cfg["offices"] = offices
    cfg["_registry"] = {
        "ok": registry.get("ok"),
        "url": registry.get("url"),
        "status": registry.get("status"),
        "error": registry.get("error"),
        "duration_s": registry.get("duration_s"),
    }
    return cfg


def _offices() -> dict[str, dict[str, Any]]:
    cfg = _merged_config()
    offices = cfg.get("offices") or {}
    if not isinstance(offices, dict):
        return {}
    return offices


def _office_or_error(office_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    oid = str(office_id or "").strip().lower()
    offices = _offices()
    office = offices.get(oid)
    if not office:
        return None, {
            "ok": False,
            "error": f"Unknown office '{office_id}'",
            "known_offices": sorted(offices),
        }
    return office, None


def _tailscale_status() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "peers": {}}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip(), "peers": {}}
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid tailscale json: {exc}", "peers": {}}

    peers: dict[str, Any] = {}
    self_node = raw.get("Self") or {}
    if self_node.get("HostName"):
        peers[str(self_node["HostName"])] = {
            "hostname": self_node.get("HostName"),
            "tailscale_ips": self_node.get("TailscaleIPs") or [],
            "online": bool(self_node.get("Online")),
            "self": True,
        }
    for peer in (raw.get("Peer") or {}).values():
        hostname = str(peer.get("HostName") or "")
        if not hostname:
            continue
        peers[hostname] = {
            "hostname": hostname,
            "tailscale_ips": peer.get("TailscaleIPs") or [],
            "online": bool(peer.get("Online")),
            "os": peer.get("OS"),
            "tags": peer.get("Tags"),
            "self": False,
        }
    return {"ok": True, "peers": peers}


def _peer_for_office(office: dict[str, Any], ts: dict[str, Any] | None = None) -> dict[str, Any]:
    ts = ts or _tailscale_status()
    host = str(office.get("tailscale_host") or "")
    ip = str(office.get("tailscale_ip") or "")
    peers = ts.get("peers") or {}
    if host and host in peers:
        return peers[host]
    for peer in peers.values():
        ips = peer.get("tailscale_ips") or []
        if ip and ip in ips:
            return peer
    return {"hostname": host, "tailscale_ips": [ip] if ip else [], "online": None}


def _tcp_probe(host: str, port: int, timeout_s: float = 2.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return {
                "ok": True,
                "host": host,
                "port": port,
                "duration_s": round(time.monotonic() - started, 3),
            }
    except Exception as exc:
        return {
            "ok": False,
            "host": host,
            "port": port,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }


def _http_probe(
    url: str,
    timeout_s: float = 5.0,
    bearer_token: str | None = None,
    max_bytes: int = 65536,
) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "no url configured"}
    started = time.monotonic()
    headers = {"Accept": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read(max_bytes).decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = None
            return {
                "ok": True,
                "method": "GET",
                "status": resp.status,
                "body_json": parsed,
                "body_preview": body[:240],
                "duration_s": round(time.monotonic() - started, 3),
            }
    except urllib.error.HTTPError as exc:
        # HTTP 405/501 on GET still proves the endpoint is reachable. The
        # delegation contract itself is POST-only and is validated by
        # openclaw_delegate_task.
        body = exc.read(240).decode("utf-8", errors="replace")
        post_only_expected = exc.code in (405, 501)
        return {
            "ok": True,
            "method": "GET",
            "reachable": True,
            "status": exc.code,
            "post_only_expected": post_only_expected,
            "diagnosis": (
                "GET rejected as expected for a POST-only delegate endpoint; "
                "use openclaw_delegate_task for the real delegation test."
                if post_only_expected
                else "Endpoint is reachable but returned an HTTP error to the GET probe."
            ),
            "body_preview": body,
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }


def _http_json_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    bearer_token: str | None = None,
    timeout_s: float = 10.0,
    max_bytes: int = 262144,
) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "no url configured"}
    started = time.monotonic()
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read(max_bytes).decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(text) if text else None
            except json.JSONDecodeError:
                parsed = {"raw": text[:1024]}
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "body_json": parsed,
                "duration_s": round(time.monotonic() - started, 3),
            }
    except urllib.error.HTTPError as exc:
        text = exc.read(min(max_bytes, 4096)).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "error": text or str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }


def _orchestration_base_url() -> str:
    return str(
        _env("HERMES_ORCHESTRATION_API_URL")
        or DEFAULT_ORCHESTRATION_API_URL
    ).rstrip("/")


def _orchestration_api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    token = _env("HERMES_ORCHESTRATION_API_KEY")
    if not token:
        return {"ok": False, "error": "missing HERMES_ORCHESTRATION_API_KEY"}
    url = f"{_orchestration_base_url()}/{path.lstrip('/')}"
    result = _http_json_request(
        method=method,
        url=url,
        payload=payload,
        bearer_token=token,
        timeout_s=timeout_s,
    )
    return {
        "ok": bool(result.get("ok")),
        "endpoint": url,
        "http_status": result.get("status"),
        "duration_s": result.get("duration_s"),
        "result": result.get("body_json"),
        "error": result.get("error"),
    }


def _metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _nested_dict(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}


def _regex_id(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _looks_like_work_order_id(value: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    if candidate.startswith(("wo_", "work_order_")):
        return True
    # Legacy Factory ids use project-id:stage-key. Avoid accepting prose values
    # such as "codex" from "work order: codex, claude_code, ...".
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$", candidate))


def _first_work_order_id(*values: Any) -> str:
    for value in values:
        candidate = str(value or "").strip()
        if _looks_like_work_order_id(candidate):
            return candidate
    return ""


def _orchestration_refs_from_task(
    task: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    metadata = _metadata_from_payload(payload)
    orchestration = _nested_dict(metadata, "orchestration")
    delegation_context = _nested_dict(metadata, "delegation_context")
    workflow_run_id = str(
        metadata.get("workflow_run_id")
        or orchestration.get("workflow_run_id")
        or ""
    ).strip()
    work_order_id = _first_work_order_id(
        metadata.get("work_order_id")
        or "",
        metadata.get("source_work_order_id") or "",
        delegation_context.get("work_order_id") or "",
        delegation_context.get("initial_work_order_id") or "",
        orchestration.get("work_order_id") or "",
        orchestration.get("initial_work_order_id") or "",
    )

    if not workflow_run_id:
        workflow_run_id = _regex_id(r"\bWORKFLOW_RUN_ID\s*:\s*([A-Za-z0-9_.-]+)", task)
    if not work_order_id:
        work_order_id = _first_work_order_id(
            _regex_id(r"\bWORK_ORDER_ID\s*:\s*([A-Za-z0-9_.:-]+)", task),
            _regex_id(r"\bWORK\s+ORDER\s+ID\s*:\s*([A-Za-z0-9_.:-]+)", task),
            _regex_id(r"\bwork_order_id\b\s*[=:]\s*([A-Za-z0-9_.:-]+)", task),
        )
    return workflow_run_id, work_order_id


def _sync_orchestration_delegation_acceptance(
    *,
    task_text: str,
    request_payload: dict[str, Any],
    office_id: str,
    agent_id: str,
    branch_task_id: str,
    async_mode: bool,
    response: Any,
    transport: str = "branch-delegation-v1",
    heartbeat_notes: str = "Branch receiver accepted delegated work.",
) -> dict[str, Any]:
    workflow_run_id, work_order_id = _orchestration_refs_from_task(
        task_text,
        request_payload,
    )
    if not workflow_run_id and not work_order_id:
        return {
            "ok": True,
            "skipped": True,
            "reason": "No WORKFLOW_RUN_ID or WORK ORDER reference found.",
        }

    result: dict[str, Any] = {
        "ok": True,
        "workflow_run_id": workflow_run_id,
        "work_order_id": work_order_id,
        "branch_task_id": branch_task_id,
    }
    metrics = {
        "office_id": office_id,
        "agent_id": agent_id,
        "branch_task_id": branch_task_id,
        "async_mode": async_mode,
        "transport": transport,
    }

    if work_order_id:
        dispatch = _orchestration_api_request(
            "POST",
            f"/v1/work-orders/{quote(work_order_id)}/dispatch",
            payload={"actor": "zeus-delegation-router", "metrics": metrics},
            timeout_s=10.0,
        )
        heartbeat = _orchestration_api_request(
            "POST",
            f"/v1/work-orders/{quote(work_order_id)}/heartbeat",
            payload={
                "actor": agent_id,
                "metrics": metrics,
                "notes": heartbeat_notes,
            },
            timeout_s=10.0,
        )
        result["dispatch"] = dispatch
        result["heartbeat"] = heartbeat
        result["ok"] = bool(heartbeat.get("ok") or dispatch.get("ok"))

    if workflow_run_id:
        event = _orchestration_api_request(
            "POST",
            "/v1/webhooks/factory-event",
            payload={
                "workflow_run_id": workflow_run_id,
                "event_type": "delegation.accepted",
                "actor": "zeus-delegation-router",
                "work_order_id": work_order_id or None,
                "idempotency_key": f"delegation.accepted:{office_id}:{branch_task_id}",
                "payload": {
                    "office_id": office_id,
                    "agent_id": agent_id,
                    "branch_task_id": branch_task_id,
                    "async_mode": async_mode,
                    "transport": transport,
                    "response": response if isinstance(response, dict) else {},
                },
            },
            timeout_s=10.0,
        )
        result["event"] = event
        result["ok"] = bool(result.get("ok") and event.get("ok"))

    return result


def _sync_orchestration_delegation_failure(
    *,
    task_text: str,
    request_payload: dict[str, Any],
    office_id: str,
    agent_id: str,
    branch_task_id: str,
    error: str,
) -> dict[str, Any]:
    workflow_run_id, work_order_id = _orchestration_refs_from_task(
        task_text,
        request_payload,
    )
    if not workflow_run_id:
        return {
            "ok": True,
            "skipped": True,
            "reason": "No WORKFLOW_RUN_ID reference found.",
        }
    return _orchestration_api_request(
        "POST",
        f"/v1/workflow-runs/{quote(workflow_run_id)}/interventions",
        payload={
            "actor": "zeus-delegation-router",
            "reason": (
                f"Delegation to {office_id}/{agent_id} failed for task "
                f"{branch_task_id}: {error[:500]}"
            ),
            "action": "blocked",
            "work_order_id": work_order_id or None,
            "notes": "Recorded automatically by openclaw_delegate_task.",
        },
        timeout_s=10.0,
    )


def _notion_state_path() -> Path:
    return Path(_env("OPENCLAW_NOTION_STATE", str(DEFAULT_NOTION_STATE))).expanduser()


def _load_notion_state() -> dict[str, Any]:
    path = _notion_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_notion_state(data: dict[str, Any]) -> None:
    path = _notion_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _notion_token() -> str:
    return _env("NOTION_API_KEY") or _env("NOTION_TOKEN") or _env("NOTION_INTEGRATION_TOKEN")


def _notion_version() -> str:
    return _env("NOTION_VERSION", DEFAULT_NOTION_VERSION)


def _notion_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 12.0,
) -> dict[str, Any]:
    token = _notion_token()
    if not token:
        return {
            "ok": False,
            "error": "missing NOTION_API_KEY, NOTION_TOKEN, or NOTION_INTEGRATION_TOKEN",
        }
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Notion-Version": _notion_version(),
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    started = time.monotonic()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read(524288).decode("utf-8", errors="replace")
            parsed = json.loads(text) if text else None
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "body_json": parsed,
                "duration_s": round(time.monotonic() - started, 3),
            }
    except urllib.error.HTTPError as exc:
        text = exc.read(8192).decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = {"raw": text}
        return {
            "ok": False,
            "status": exc.code,
            "error": parsed or str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }


def _notion_text(content: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": str(content)[:2000]}}


def _notion_paragraph(content: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_notion_text(content)]}}


def _notion_heading(content: str, level: int = 2) -> dict[str, Any]:
    block_type = "heading_3" if level == 3 else "heading_2"
    return {"object": "block", "type": block_type, block_type: {"rich_text": [_notion_text(content)]}}


def _notion_bullets(items: list[str]) -> list[dict[str, Any]]:
    return [
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [_notion_text(item)]}}
        for item in items
        if str(item).strip()
    ]


def _notion_title_from_page(page: dict[str, Any]) -> str:
    properties = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    for value in properties.values():
        if not isinstance(value, dict) or value.get("type") != "title":
            continue
        parts = value.get("title") or []
        text = "".join(str(part.get("plain_text") or "") for part in parts if isinstance(part, dict)).strip()
        if text:
            return text
    return str(page.get("id") or "untitled")


def _notion_search_pages(query: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"filter": {"property": "object", "value": "page"}, "page_size": 25}
    if query:
        payload["query"] = query
    result = _notion_request("POST", "search", payload=payload)
    if not result.get("ok"):
        return result
    body = result.get("body_json") if isinstance(result.get("body_json"), dict) else {}
    pages = []
    for page in body.get("results") or []:
        if not isinstance(page, dict):
            continue
        pages.append(
            {
                "id": page.get("id"),
                "title": _notion_title_from_page(page),
                "url": page.get("url"),
                "last_edited_time": page.get("last_edited_time"),
            }
        )
    return {"ok": True, "pages": pages, "duration_s": result.get("duration_s")}


def _notion_create_page(
    parent_page_id: str,
    title: str,
    children: list[dict[str, Any]] | None = None,
    workspace_parent: bool = False,
) -> dict[str, Any]:
    parent = (
        {"type": "workspace", "workspace": True}
        if workspace_parent
        else {"type": "page_id", "page_id": parent_page_id}
    )
    payload = {
        "parent": parent,
        "properties": {"title": {"title": [_notion_text(title)]}},
    }
    if children:
        payload["children"] = children[:100]
    result = _notion_request("POST", "pages", payload=payload)
    if not result.get("ok"):
        return result
    page = result.get("body_json") if isinstance(result.get("body_json"), dict) else {}
    return {
        "ok": True,
        "id": page.get("id"),
        "title": title,
        "url": page.get("url"),
        "duration_s": result.get("duration_s"),
    }


def _notion_append_children(block_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    if not block_id:
        return {"ok": False, "error": "block_id is required"}
    if not children:
        return {"ok": True, "status": "no_children"}
    result = _notion_request("PATCH", f"blocks/{block_id}/children", {"children": children[:100]})
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "block_id": block_id,
        "duration_s": result.get("duration_s"),
    }


def _notion_children(parent_id: str, page_size: int = 100) -> dict[str, Any]:
    if not parent_id:
        return {"ok": False, "error": "parent_id is required"}
    return _notion_request("GET", f"blocks/{parent_id}/children?page_size={max(1, min(page_size, 100))}")


def _notion_child_page_by_title(parent_id: str, title: str) -> dict[str, Any] | None:
    result = _notion_children(parent_id)
    if not result.get("ok"):
        return None
    body = result.get("body_json") if isinstance(result.get("body_json"), dict) else {}
    wanted = str(title or "").strip()
    for block in body.get("results") or []:
        if not isinstance(block, dict) or block.get("type") != "child_page":
            continue
        child = block.get("child_page") if isinstance(block.get("child_page"), dict) else {}
        if str(child.get("title") or "").strip() == wanted:
            return {
                "ok": True,
                "id": block.get("id"),
                "title": wanted,
            }
    return None


def _notion_ensure_page(parent_id: str, title: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    existing = _notion_child_page_by_title(parent_id, title)
    if existing:
        return {**existing, "created": False}
    created = _notion_create_page(parent_id, title, children)
    return {**created, "created": bool(created.get("ok"))}


def _notion_section_id(state: dict[str, Any], section_name: str) -> str:
    section = (state.get("sections") or {}).get(section_name) or {}
    return str(section.get("id") or "").strip()


def _duration_text(duration_s: int | float | str | None) -> str:
    try:
        total = max(0, int(float(duration_s or 0)))
    except Exception:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _notion_board_page_id(explicit_page_id: str = "") -> str:
    state = _load_notion_state()
    return (
        str(explicit_page_id or "").strip()
        or _env("NOTION_SITIOUNO_BOARD_PAGE_ID")
        or str(state.get("board_page_id") or "").strip()
        or _env("NOTION_SITIOUNO_PARENT_PAGE_ID")
    )


def _openhands_runner() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    office, err = _office_or_error("openhands_runner")
    if err:
        return None, err
    return office, None


def _openhands_connector_url(office: dict[str, Any]) -> str:
    services = office.get("services") if isinstance(office.get("services"), dict) else {}
    openhands = services.get("openhands") if isinstance(services.get("openhands"), dict) else {}
    return str(
        _env("OPENHANDS_CONNECTOR_URL")
        or openhands.get("connector_url")
        or office.get("api_base_url")
        or ""
    ).rstrip("/")


def _openhands_connector_token(office: dict[str, Any]) -> tuple[str, str]:
    token_env = str(office.get("connector_token_env") or "OPENHANDS_CONNECTOR_TOKEN")
    return token_env, _env(token_env)


def _openhands_runner_task_id(result: dict[str, Any]) -> str:
    body = result.get("body_json") if isinstance(result.get("body_json"), dict) else {}
    candidates: list[Any] = [
        body.get("task_id"),
        body.get("start_task_id"),
        body.get("id"),
    ]
    nested = body.get("result") if isinstance(body.get("result"), dict) else {}
    nested_body = nested.get("body") if isinstance(nested.get("body"), dict) else {}
    candidates.extend(
        [
            nested.get("task_id"),
            nested.get("start_task_id"),
            nested.get("id"),
            nested_body.get("id"),
            nested_body.get("start_task_id"),
            nested_body.get("task_id"),
        ]
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _endpoint_host_port(endpoint: str) -> tuple[str, int] | None:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(endpoint)
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, int(port)
    except Exception:
        return None


def _redacted_office(office_id: str, office: dict[str, Any], include_live: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": office_id,
        "display_name": office.get("display_name"),
        "kind": office.get("kind"),
        "status": office.get("status"),
        "gcp_project": office.get("gcp_project"),
        "gcp_zone": office.get("gcp_zone"),
        "gcp_instance": office.get("gcp_instance"),
        "gcp_internal_ip": office.get("gcp_internal_ip"),
        "tailscale_host": office.get("tailscale_host"),
        "tailscale_ip": office.get("tailscale_ip"),
        "ui_url": office.get("ui_url"),
        "api_base_url": office.get("api_base_url"),
        "health_endpoint": office.get("health_endpoint"),
        "delegate_endpoint": office.get("delegate_endpoint"),
        "token_env": office.get("token_env"),
        "token_configured": bool(_env(str(office.get("token_env") or ""))),
        "contract": office.get("contract"),
        "agents": office.get("agents") or {},
        "services": office.get("services") or {},
        "capabilities": office.get("capabilities") or [],
        "repos": office.get("repos") or {},
        "notes": office.get("notes") or [],
    }
    if include_live:
        peer = _peer_for_office(office)
        item["tailscale"] = peer
        health_endpoint = str(office.get("health_endpoint") or "")
        if health_endpoint:
            item["health_probe"] = _http_probe(health_endpoint)
        endpoint = str(office.get("delegate_endpoint") or "")
        if endpoint:
            token_env = str(office.get("token_env") or "")
            token = _env(token_env) if token_env else None
            item["delegate_probe"] = _http_probe(endpoint, bearer_token=token)
            if item["delegate_probe"].get("post_only_expected"):
                item["delegate_endpoint_note"] = (
                    "HTTP 405/501 on the GET probe is expected here because "
                    "/v1/delegate is POST-only. Validate execution with "
                    "openclaw_delegate_task."
                )
    return item


@mcp.tool()
def openclaw_identity() -> dict[str, Any]:
    """Return Hermes' SitioUno/OpenClaw identity and the control-plane pattern."""
    cfg = _merged_config()
    return {
        "ok": True,
        "site": cfg.get("site") or {},
        "registry": cfg.get("_registry") or {},
        "pattern": {
            "identity_source": "~/.hermes/SOUL.md",
            "office_registry": str(_config_path()),
            "live_registry": _registry_base_url(cfg),
            "delegation_contract": "branch-delegation-v1",
            "transport": "HTTP POST /v1/delegate over Tailscale or approved private network",
            "security": "Bearer token per office via environment variable; no secrets in YAML",
        },
    }


@mcp.tool()
def openclaw_list_offices(include_live: bool = True) -> dict[str, Any]:
    """List known OpenClaw offices, agents, endpoints, and live Tailscale status."""
    cfg = _merged_config()
    offices = cfg.get("offices") or {}
    return {
        "ok": True,
        "site": cfg.get("site") or {},
        "config_path": str(_config_path()),
        "registry": cfg.get("_registry") or {},
        "offices": {
            office_id: _redacted_office(office_id, office, include_live=include_live)
            for office_id, office in sorted(offices.items())
        },
    }


@mcp.tool()
def openclaw_registry_branches() -> dict[str, Any]:
    """Return the live HQ registry branch inventory without delegation secrets."""
    cfg = _load_config()
    registry = _registry_branches(cfg)
    branches = []
    for row in registry.get("branches") or []:
        if not isinstance(row, dict):
            continue
        branches.append(
            {
                "branch_id": row.get("branch_id"),
                "display_name": row.get("display_name"),
                "node_id": row.get("node_id"),
                "online_hint": row.get("online_hint"),
                "last_heartbeat": row.get("last_heartbeat"),
                "updated_at": row.get("updated_at"),
                "agent_count": len(row.get("agents") or []),
                "agents": _registry_agents(row),
                "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
            }
        )
    return {
        "ok": bool(registry.get("ok")),
        "url": registry.get("url"),
        "status": registry.get("status"),
        "error": registry.get("error"),
        "duration_s": registry.get("duration_s"),
        "branches": branches,
    }


@mcp.tool()
def openclaw_office_status(office_id: str) -> dict[str, Any]:
    """Inspect one office: tailnet status, delegate endpoint reachability, token presence, agents."""
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    status = _redacted_office(str(office_id).strip().lower(), office, include_live=True)
    endpoint = str(office.get("delegate_endpoint") or "")
    hp = _endpoint_host_port(endpoint) if endpoint else None
    if hp:
        status["tcp_probe"] = _tcp_probe(hp[0], hp[1])
    return {"ok": True, "office": status}


@mcp.tool()
def openclaw_branch_report(office_id: str) -> dict[str, Any]:
    """Fetch the branch's first-hand GET report, including its local Kanban summary."""
    office_id = str(office_id or "").strip().lower()
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token:
        return {"ok": False, "error": f"missing delegation token env {token_env}", "token_env": token_env}
    result = _http_probe(endpoint, timeout_s=10.0, bearer_token=token, max_bytes=262144)
    report = result.get("body_json")
    factory_feedback = _factory_feedback_summary(report) if isinstance(report, dict) else None
    return {
        "ok": bool(result.get("ok") and result.get("status") == 200 and isinstance(report, dict)),
        "office_id": office_id,
        "endpoint": endpoint,
        "http_status": result.get("status"),
        "duration_s": result.get("duration_s"),
        "report": report if isinstance(report, dict) else None,
        "factory_feedback": factory_feedback,
        "probe": result if not isinstance(report, dict) else None,
    }


def _factory_feedback_summary(report: dict[str, Any]) -> dict[str, Any]:
    factory = report.get("factory_workflow") if isinstance(report, dict) else {}
    runs = factory.get("runs") if isinstance(factory, dict) else []
    active_blockers: list[dict[str, Any]] = []
    waiting_approvals: list[dict[str, Any]] = []
    for run in (runs if isinstance(runs, list) else []):
        if not isinstance(run, dict):
            continue
        gate_review = run.get("gate_review") if isinstance(run.get("gate_review"), dict) else None
        if gate_review:
            waiting_approvals.append(
                {
                    "project_id": run.get("project_id"),
                    "state": run.get("state"),
                    "reviewer_role": gate_review.get("reviewer_role"),
                    "requested_by": gate_review.get("requested_by"),
                    "reason": gate_review.get("reason"),
                    "evidence_paths": gate_review.get("evidence_paths") or [],
                }
            )
        blocked_reason = run.get("blocked_reason")
        if run.get("status") in {"blocked", "waiting_approval"} or blocked_reason:
            active_blockers.append(
                {
                    "project_id": run.get("project_id"),
                    "state": run.get("state"),
                    "status": run.get("status"),
                    "blocked_reason": blocked_reason,
                    "current_sprint_id": run.get("current_sprint_id"),
                }
            )
    return {
        "source_of_truth": "factory_workflow block from the branch report",
        "zeus_action": (
            "Use this summary to supervise and decide; do not bypass branch owners "
            "or ask Leo to close specialist work directly. For approvals, holds, "
            "or requested changes, first read evidence with openclaw_factory_artifact_get, "
            "then use openclaw_factory_gate_decision."
        ),
        "active_blockers": active_blockers,
        "waiting_approvals": waiting_approvals,
        "interlocutors": {
            "branch_status": "ana-pmo",
            "orchestration": "leo-orquestador",
            "discovery": "vera-research",
            "product": "mia-producto",
            "architecture": "nico-arquitecto",
            "execution": "ciro-codex / clara-claude / olga-openhands",
            "review": "bruno-integrador",
            "qa": "tina-qa / belen-browser",
            "security": "sofia-secdevops",
            "release": "rene-release",
            "docs_memory": "dario-docs",
        },
    }


def _branch_factory_post(
    office_id: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    office_id = str(office_id or "").strip().lower()
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token:
        return {"ok": False, "error": f"missing delegation token env {token_env}", "token_env": token_env}
    base = endpoint.rsplit("/v1/delegate", 1)[0].rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    result = _http_json_request(
        "POST",
        url,
        payload=payload,
        bearer_token=token,
        timeout_s=timeout_s,
    )
    return {
        "ok": bool(result.get("ok")),
        "office_id": office_id,
        "endpoint": url,
        "http_status": result.get("status"),
        "result": result.get("body_json"),
        "error": result.get("error"),
        "duration_s": result.get("duration_s"),
    }


def _branch_factory_get(
    office_id: str,
    path: str,
    *,
    timeout_s: float = 20.0,
    max_bytes: int = 262144,
) -> dict[str, Any]:
    office_id = str(office_id or "").strip().lower()
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token:
        return {"ok": False, "error": f"missing delegation token env {token_env}", "token_env": token_env}
    base = endpoint.rsplit("/v1/delegate", 1)[0].rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    result = _http_json_request(
        "GET",
        url,
        bearer_token=token,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
    )
    return {
        "ok": bool(result.get("ok")),
        "office_id": office_id,
        "endpoint": url,
        "http_status": result.get("status"),
        "result": result.get("body_json"),
        "error": result.get("error"),
        "duration_s": result.get("duration_s"),
    }


@mcp.tool()
def openclaw_factory_gate_decision(
    project_id: str,
    state: str,
    decision: str,
    notes: str = "",
    office_id: str = "sicilia",
    reviewer_role: str = "zeus",
    dispatch_next: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Approve, hold, reject, or request changes on a Factory stage gate."""
    office_id = str(office_id or "sicilia").strip().lower()
    project_id = str(project_id or "").strip()
    state = str(state or "").strip().upper()
    decision = str(decision or "").strip().lower()
    reviewer_role = str(reviewer_role or "zeus").strip() or "zeus"
    notes = str(notes or "").strip()
    if not project_id:
        return {"ok": False, "error": "project_id is required"}
    if not state:
        return {"ok": False, "error": "state is required"}
    if decision not in {
        "approve",
        "approved",
        "hold",
        "pause",
        "reject",
        "rejected",
        "changes_requested",
        "request_changes",
    }:
        return {
            "ok": False,
            "error": "decision must be approve, hold, reject, or changes_requested",
        }
    payload = {
        "project_id": project_id,
        "state": state,
        "decision": decision,
        "reviewer_role": reviewer_role,
        "notes": notes,
        "branch": office_id,
        "dispatch_next": bool(dispatch_next),
    }
    if dry_run:
        office, err = _office_or_error(office_id)
        if err:
            return err
        assert office is not None
        endpoint = str(office.get("delegate_endpoint") or "").strip()
        base = endpoint.rsplit("/v1/delegate", 1)[0].rstrip("/") if endpoint else ""
        return {
            "ok": True,
            "dry_run": True,
            "office_id": office_id,
            "endpoint": f"{base}/v1/factory/gate-decision" if base else None,
            "payload": payload,
            "usage": "Set dry_run=false after checking openclaw_branch_report.factory_feedback.",
        }
    result = _branch_factory_post(
        office_id,
        "/v1/factory/gate-decision",
        payload,
        timeout_s=20.0,
    )
    return {
        **result,
        "project_id": project_id,
        "state": state,
        "decision": decision,
        "next_poll": {
            "tool": "openclaw_branch_report",
            "office_id": office_id,
            "reason": "branch report is canonical for Factory state, hold/blocker status, and next gate",
        },
    }


@mcp.tool()
def openclaw_factory_artifact_list(
    project_id: str,
    office_id: str = "sicilia",
) -> dict[str, Any]:
    """List Markdown artifacts available for a Factory project on a branch."""
    office_id = str(office_id or "sicilia").strip().lower()
    project_id = str(project_id or "").strip()
    if not project_id:
        return {"ok": False, "error": "project_id is required"}
    query = urlencode({"project_id": project_id})
    return _branch_factory_get(
        office_id,
        f"/v1/factory/artifacts?{query}",
        timeout_s=20.0,
        max_bytes=262144,
    )


@mcp.tool()
def openclaw_factory_artifact_get(
    project_id: str,
    path: str,
    office_id: str = "sicilia",
    max_bytes: int = 120000,
) -> dict[str, Any]:
    """Read one Markdown artifact from a Factory project on a branch."""
    office_id = str(office_id or "sicilia").strip().lower()
    project_id = str(project_id or "").strip()
    path = str(path or "").strip()
    if not project_id:
        return {"ok": False, "error": "project_id is required"}
    if not path:
        return {"ok": False, "error": "path is required"}
    max_bytes = max(1, min(int(max_bytes or 120000), 262144))
    query = urlencode({"project_id": project_id, "path": path, "max_bytes": str(max_bytes)})
    return _branch_factory_get(
        office_id,
        f"/v1/factory/artifacts?{query}",
        timeout_s=20.0,
        max_bytes=max_bytes + 4096,
    )


@mcp.tool()
def openclaw_factory_artifact_put(
    project_id: str,
    path: str,
    content: str,
    office_id: str = "sicilia",
    produced_by: str = "zeus",
    description: str = "",
    overwrite: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write a Markdown feedback/evidence artifact into a Factory project."""
    office_id = str(office_id or "sicilia").strip().lower()
    project_id = str(project_id or "").strip()
    path = str(path or "").strip()
    content = str(content or "")
    produced_by = str(produced_by or "zeus").strip() or "zeus"
    if not project_id:
        return {"ok": False, "error": "project_id is required"}
    if not path:
        return {"ok": False, "error": "path is required"}
    if not path.lower().endswith(".md"):
        return {"ok": False, "error": "only .md artifacts are supported"}
    if not content.strip():
        return {"ok": False, "error": "content is required"}
    payload = {
        "project_id": project_id,
        "path": path,
        "content": content,
        "produced_by": produced_by,
        "description": description or "Zeus feedback/evidence artifact.",
        "overwrite": bool(overwrite),
        "register": True,
    }
    if dry_run:
        office, err = _office_or_error(office_id)
        if err:
            return err
        assert office is not None
        endpoint = str(office.get("delegate_endpoint") or "").strip()
        base = endpoint.rsplit("/v1/delegate", 1)[0].rstrip("/") if endpoint else ""
        return {
            "ok": True,
            "dry_run": True,
            "office_id": office_id,
            "endpoint": f"{base}/v1/factory/artifacts" if base else None,
            "payload_preview": {
                **payload,
                "content": content[:500],
                "content_bytes": len(content.encode("utf-8")),
            },
        }
    return _branch_factory_post(
        office_id,
        "/v1/factory/artifacts",
        payload,
        timeout_s=20.0,
    )


def _branch_kanban_write(office_id: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    office_id = str(office_id or "").strip().lower()
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token:
        return {"ok": False, "error": f"missing delegation token env {token_env}", "token_env": token_env}
    base = endpoint.rsplit("/v1/delegate", 1)[0].rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    result = _http_json_request("POST", url, payload=payload, bearer_token=token, timeout_s=10.0)
    return {
        "ok": bool(result.get("ok")),
        "office_id": office_id,
        "endpoint": url,
        "http_status": result.get("status"),
        "result": result.get("body_json"),
        "error": result.get("error"),
    }


def _branch_kanban_read(office_id: str, path: str = "/v1/kanban") -> dict[str, Any]:
    office_id = str(office_id or "").strip().lower()
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token:
        return {"ok": False, "error": f"missing delegation token env {token_env}", "token_env": token_env}
    base = endpoint.rsplit("/v1/delegate", 1)[0].rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    result = _http_json_request("GET", url, bearer_token=token, timeout_s=10.0)
    return {
        "ok": bool(result.get("ok")),
        "office_id": office_id,
        "endpoint": url,
        "http_status": result.get("status"),
        "result": result.get("body_json"),
        "error": result.get("error"),
    }


def _kanban_snapshot_task_ids(snapshot: Any) -> set[str]:
    task_ids: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("id", "task_id"):
                raw = value.get(key)
                if isinstance(raw, str) and raw:
                    task_ids.add(raw)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(snapshot)
    return task_ids


@mcp.tool()
def openclaw_openhands_status() -> dict[str, Any]:
    """Inspect the private OpenHands runner and explain the approved routing model."""
    office, err = _openhands_runner()
    if err:
        return err
    assert office is not None
    connector_url = _openhands_connector_url(office)
    token_env, token = _openhands_connector_token(office)
    status = _redacted_office("openhands_runner", office, include_live=True)
    connector_status: dict[str, Any] = {
        "configured": bool(connector_url),
        "url": connector_url or None,
        "token_env": token_env,
        "token_configured": bool(token),
    }
    if connector_url and token:
        connector_status["health"] = _http_json_request(
            "GET",
            urljoin(f"{connector_url}/", "v1/openhands/status"),
            bearer_token=token,
            timeout_s=12.0,
        )
    return {
        "ok": True,
        "runner": status,
        "connector": connector_status,
        "routing_policy": {
            "default_route": "factory",
            "factory_path": "Zeus -> Sicilia -> olga-openhands -> OpenHands runner",
            "vm_route": "approved execution surface for OpenHands work: route='vm' or route='runner'",
            "cli_fallback": (
                "openhands_cli is a separate fallback/benchmark engine and must be "
                "explicitly recorded; it must not silently replace openhands_vm."
            ),
            "direct_connector": "break-glass only; disabled for execute=true unless explicitly enabled",
            "engine_portfolio": {
                "codex": "fast repo edits, tests, docs, focused implementation",
                "claude_code": "reasoning-heavy implementation and refactors",
                "openhands_vm": "dedicated OpenHands runner VM/service for autonomous implementation",
                "openhands_cli": "explicit fallback or benchmark path, never implicit VM substitute",
            },
            "metrics_required": [
                "engine_id",
                "execution_surface",
                "duration_s",
                "runner_task_id_or_branch_task_id",
                "commit_sha",
                "changed_files",
                "error_type",
                "regate_count",
                "accepted_by_gate",
            ],
            "non_overlap_rule": (
                "Zeus should keep product intent, supervision, and escalation. "
                "Factory/olga-openhands owns development execution."
            ),
        },
    }


@mcp.tool()
def openclaw_openhands_task_status(task_id: str) -> dict[str, Any]:
    """Inspect one OpenHands runner VM task created through the connector."""
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    office, err = _openhands_runner()
    if err:
        return err
    assert office is not None
    connector_url = _openhands_connector_url(office)
    token_env, token = _openhands_connector_token(office)
    if not connector_url:
        return {"ok": False, "error": "OpenHands connector URL is not configured"}
    if not token:
        return {"ok": False, "error": f"missing OpenHands connector token env {token_env}", "token_env": token_env}
    result = _http_json_request(
        "GET",
        urljoin(f"{connector_url}/", f"v1/openhands/tasks/{quote(task_id)}"),
        bearer_token=token,
        timeout_s=12.0,
    )
    return {
        "ok": bool(result.get("ok")),
        "task_id": task_id,
        "runner": "openhands_runner",
        "connector_url": connector_url,
        "token_env": token_env,
        "token_configured": bool(token),
        "result": result,
    }


@mcp.tool()
def openclaw_notion_status(query: str = "SitioUno") -> dict[str, Any]:
    """Validate Zeus' Notion integration and list shared pages visible to the integration."""
    token_configured = bool(_notion_token())
    state = _load_notion_state()
    if not token_configured:
        return {
            "ok": False,
            "token_configured": False,
            "error": "missing NOTION_API_KEY, NOTION_TOKEN, or NOTION_INTEGRATION_TOKEN",
            "state_path": str(_notion_state_path()),
            "state": state,
        }
    me = _notion_request("GET", "users/me")
    search = _notion_search_pages(str(query or ""))
    return {
        "ok": bool(me.get("ok")),
        "token_configured": True,
        "notion_version": _notion_version(),
        "bot": me.get("body_json") if me.get("ok") else None,
        "bot_error": me.get("error") if not me.get("ok") else None,
        "search": search,
        "state_path": str(_notion_state_path()),
        "state": state,
        "board_page_id": _notion_board_page_id(),
    }


@mcp.tool()
def openclaw_notion_create_structure(
    parent_page_id: str = "",
    root_title: str = "SitioUno Operating Board",
    remember: bool = True,
) -> dict[str, Any]:
    """Create Zeus' Notion operating structure under a shared parent page."""
    parent = str(parent_page_id or "").strip() or _env("NOTION_SITIOUNO_PARENT_PAGE_ID")
    workspace_parent = False
    if not parent:
        search = _notion_search_pages("SitioUno")
        pages = search.get("pages") if isinstance(search.get("pages"), list) else []
        if len(pages) == 1:
            parent = str(pages[0].get("id") or "")
        else:
            parent = "workspace"
            workspace_parent = True

    root_children = [
        _notion_paragraph(
            "Human-readable operating mirror for Zeus, OpenClaw offices, and the SitioUno Software Factory."
        ),
        _notion_heading("Operating Rule"),
        _notion_paragraph(
            "Notion is the documentation mirror for Jean. Operational truth remains in branch reports, local Kanban, repos, and deliverables."
        ),
        _notion_heading("When Zeus Writes Here"),
        *_notion_bullets(
            [
                "New initiative accepted for shaping or execution.",
                "Factory delegation created, advanced, blocked, accepted, or rejected.",
                "Preview Lab URL published for Jean review.",
                "Decision, risk, scope change, sprint opening, sprint close, QA evidence, or retrospective recorded.",
                "Cross-office status summary needs a durable human-readable trace.",
            ]
        ),
    ]
    root = _notion_create_page(
        parent,
        str(root_title or "SitioUno Operating Board"),
        root_children,
        workspace_parent=workspace_parent,
    )
    if not root.get("ok"):
        return {
            **root,
            "diagnosis": (
                "Notion token is valid, but this integration cannot create a top-level "
                "workspace page. Share a Notion parent page with the integration, then "
                "run openclaw_notion_create_structure(parent_page_id='<page_id>')."
            )
            if workspace_parent
            else "Notion rejected page creation under the requested parent page.",
            "set_env": "NOTION_SITIOUNO_PARENT_PAGE_ID=<page_id>",
        }

    root_id = str(root.get("id") or "")
    section_specs = {
        "Projects": [
            "One page per initiative or product.",
            "Link PRD, architecture brief, preview URLs, final acceptance, and retrospective.",
        ],
        "Sprints": [
            "Sprint opening, scope, commitments, review notes, and close summary.",
            "Use this as the human mirror; branch Kanban remains the operational board.",
        ],
        "Tasks and Delegations": [
            "High-level delegated work only.",
            "Do not duplicate every worker ticket from a branch.",
        ],
        "Decisions": [
            "Record product, architecture, security, and release decisions that should survive chat history.",
        ],
        "Risks and Blockers": [
            "Record escalations that need Jean, Zeus, or Factory Director attention.",
        ],
        "Deliverables and Previews": [
            "Record Preview Lab URLs, owner, acceptance criteria, and review outcome.",
        ],
        "QA and Evidence": [
            "Record Playwright evidence, smoke tests, bugs, screenshots, and validation notes.",
        ],
        "Retrospectives": [
            "Record what worked, what failed, and what should be memorized or changed in SOPs.",
        ],
        "Branch Reports": [
            "Snapshots from openclaw_branch_report for Sicilia, Miami, HQ, and future offices.",
        ],
    }

    sections: dict[str, Any] = {}
    for title, bullets in section_specs.items():
        page = _notion_create_page(
            root_id,
            title,
            [
                _notion_paragraph(f"{title} workspace for Zeus and SitioUno operations."),
                _notion_heading("Usage", level=3),
                *_notion_bullets(bullets),
            ],
        )
        sections[title] = page

    state = _load_notion_state()
    state.update(
        {
            "board_page_id": root_id,
            "board_title": root.get("title"),
            "board_url": root.get("url"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parent": {"type": "workspace" if workspace_parent else "page_id", "id": parent},
            "sections": {
                title: {"id": page.get("id"), "url": page.get("url"), "ok": page.get("ok")}
                for title, page in sections.items()
            },
        }
    )
    if remember:
        _save_notion_state(state)

    return {
        "ok": all(bool(page.get("ok")) for page in sections.values()),
        "root": root,
        "sections": sections,
        "remembered": bool(remember),
        "state_path": str(_notion_state_path()),
        "state": state,
    }


@mcp.tool()
def openclaw_notion_log_event(
    title: str,
    summary: str,
    category: str = "operations",
    status: str = "info",
    preview_url: str = "",
    board_page_id: str = "",
) -> dict[str, Any]:
    """Write an operational event to Zeus' Notion operating board."""
    page_id = _notion_board_page_id(board_page_id)
    if not page_id:
        return {
            "ok": False,
            "error": "Notion board page is not configured",
            "fix": "Run openclaw_notion_create_structure or set NOTION_SITIOUNO_BOARD_PAGE_ID.",
            "state_path": str(_notion_state_path()),
        }
    title = str(title or "").strip()
    summary = str(summary or "").strip()
    if not title or not summary:
        return {"ok": False, "error": "title and summary are required"}
    children = [
        _notion_paragraph(summary),
        _notion_heading("Metadata"),
        *_notion_bullets(
            [
                f"Category: {category}",
                f"Status: {status}",
                f"Preview URL: {preview_url}" if preview_url else "",
                f"Recorded by: Zeus / openclaw-office MCP",
                f"Recorded at UTC: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            ]
        ),
    ]
    result = _notion_create_page(page_id, title, children)
    return {
        "ok": bool(result.get("ok")),
        "event": result,
        "board_page_id": page_id,
        "category": category,
        "status": status,
    }


@mcp.tool()
def openclaw_notion_create_factory_project_record(
    project_id: str,
    title: str,
    preview_url: str,
    repo_url: str,
    status: str = "pending_zeus_acceptance",
    branch_task_id: str = "",
    zeus_task_id: str = "",
    sprint_name: str = "Sprint 01 - Delivery",
    stage_summary: str = "",
    engine_summary: str = "",
    qa_summary: str = "",
    security_summary: str = "",
    release_summary: str = "",
    retrospective_summary: str = "",
    duration_seconds: int = 0,
    started_at: str = "",
    completed_at: str = "",
    decision_summary: str = "",
) -> dict[str, Any]:
    """Create a standardized Factory project mirror in the SitioUno Notion board.

    This is the canonical Notion template for Factory projects. It writes the
    same structure used by FACTORY-WEB-001: a project page under Projects,
    child pages for sprint/stages/execution/QA/release/retro, and board mirror
    pages under Sprints, Tasks, Deliverables, QA, Retrospectives, and Decisions.
    """
    project_id = _slugify(project_id, fallback="factory-project")
    title = str(title or "").strip()
    preview_url = str(preview_url or "").strip()
    repo_url = str(repo_url or "").strip()
    if not project_id or not title:
        return {"ok": False, "error": "project_id and title are required"}
    state = _load_notion_state()
    required_sections = {
        "Projects": _notion_section_id(state, "Projects"),
        "Sprints": _notion_section_id(state, "Sprints"),
        "Tasks and Delegations": _notion_section_id(state, "Tasks and Delegations"),
        "Deliverables and Previews": _notion_section_id(state, "Deliverables and Previews"),
        "QA and Evidence": _notion_section_id(state, "QA and Evidence"),
        "Retrospectives": _notion_section_id(state, "Retrospectives"),
        "Decisions": _notion_section_id(state, "Decisions"),
    }
    missing = [name for name, page_id in required_sections.items() if not page_id]
    if missing:
        return {
            "ok": False,
            "error": "Notion board structure is missing required sections",
            "missing_sections": missing,
            "fix": "Run openclaw_notion_create_structure first.",
            "state_path": str(_notion_state_path()),
        }

    project_title = f"{project_id.upper()} - {title}"
    duration = _duration_text(duration_seconds)
    stage_items = [
        "IDEA: idea_brief.md",
        "DISCOVERY: research_dossier.md",
        "PRODUCT_SHAPING: prd.md",
        "ARCHITECTURE_REVIEW: architecture_brief.md",
        "READY_FOR_SPRINT: sprint_plan.md",
        "EXECUTION: execution_trace.md",
        "CODE_REVIEW: code_review.md",
        "QA_VALIDATION: qa_validation.md and browser_qa_playwright.md",
        "SECURITY_REVIEW: security_review.md",
        "ZEUS_ACCEPTANCE: zeus_acceptance.md",
        "RELEASE: release_handoff.md",
        "RETROSPECTIVE: retrospective.md",
        "MEMORY_UPDATE: memory_update.md",
    ]
    project_page = _notion_ensure_page(
        required_sections["Projects"],
        project_title,
        [
            _notion_paragraph(f"Status: {status}."),
            _notion_heading("Executive Summary"),
            _notion_paragraph(stage_summary or "Factory project record created from the canonical Zeus -> Factory template."),
            _notion_heading("Public URLs"),
            *_notion_bullets(
                [
                    f"Preview: {preview_url}" if preview_url else "",
                    f"Repository: {repo_url}" if repo_url else "",
                ]
            ),
            _notion_heading("Metrics"),
            *_notion_bullets(
                [
                    f"Started at UTC: {started_at}" if started_at else "",
                    f"Completed at UTC: {completed_at}" if completed_at else "",
                    f"Cycle time: {duration} ({int(duration_seconds or 0)} seconds)" if duration_seconds else "",
                    f"Branch Kanban task: {branch_task_id}" if branch_task_id else "",
                    f"Zeus Kanban task: {zeus_task_id}" if zeus_task_id else "",
                ]
            ),
        ],
    )
    if not project_page.get("ok"):
        return project_page
    project_page_id = str(project_page.get("id") or "")

    child_specs = {
        sprint_name: [
            _notion_paragraph(f"Sprint goal and delivery scope for {project_title}."),
            _notion_heading("Definition Of Done"),
            *_notion_bullets(
                [
                    "Repo exists and is private.",
                    "Preview Lab URL is published when the deliverable is user-facing.",
                    "QA and security gates have explicit evidence.",
                    "Zeus Acceptance is recorded as accepted, changes_requested, or escalated.",
                ]
            ),
        ],
        "Stage Gate Board - IDEA to MEMORY_UPDATE": [
            _notion_paragraph("Standard Factory state-machine trace."),
            _notion_heading("Stages"),
            *_notion_bullets(stage_items),
        ],
        "Execution Trace - Engine and Worker Usage": [
            _notion_paragraph(engine_summary or "Engine/worker evidence must name Codex, Claude Code, OpenHands, or other executor."),
            _notion_heading("Timing"),
            *_notion_bullets(
                [
                    f"Started at UTC: {started_at}" if started_at else "",
                    f"Completed at UTC: {completed_at}" if completed_at else "",
                    f"Cycle time: {duration} ({int(duration_seconds or 0)} seconds)" if duration_seconds else "",
                ]
            ),
        ],
        "QA Evidence and Release": [
            _notion_heading("QA"),
            _notion_paragraph(qa_summary or "QA summary pending."),
            _notion_heading("Security"),
            _notion_paragraph(security_summary or "Security summary pending."),
            _notion_heading("Release"),
            _notion_paragraph(release_summary or "Release summary pending."),
            *_notion_bullets([preview_url, repo_url]),
        ],
        "Retrospective and Memory Update": [
            _notion_paragraph(retrospective_summary or "Retrospective pending."),
            _notion_heading("Memory Candidate"),
            _notion_paragraph("Persist durable routing truth, lessons, and process changes. Do not store secrets."),
        ],
    }
    project_children = {
        name: _notion_ensure_page(project_page_id, name, children)
        for name, children in child_specs.items()
    }

    board_pages = {
        "sprint": _notion_ensure_page(
            required_sections["Sprints"],
            f"{project_id.upper()} / {sprint_name}",
            [
                _notion_paragraph(f"Project: {project_title}"),
                _notion_paragraph(f"Status: {status}"),
                _notion_paragraph(f"Project page: {project_page.get('url') or project_page_id}"),
            ],
        ),
        "tasks": _notion_ensure_page(
            required_sections["Tasks and Delegations"],
            f"{project_id.upper()} - Stage Tasks and Delegation Trace",
            [
                _notion_paragraph(f"Branch Kanban task: {branch_task_id or project_id}"),
                _notion_paragraph(f"Zeus Kanban task: {zeus_task_id or 'pending'}"),
                *_notion_bullets(stage_items),
            ],
        ),
        "deliverable": _notion_ensure_page(
            required_sections["Deliverables and Previews"],
            f"{project_id.upper()} - Public Preview Deliverable",
            [
                _notion_heading("Preview"),
                _notion_paragraph(preview_url or "No public preview URL recorded."),
                _notion_heading("Repository"),
                _notion_paragraph(repo_url or "No repository URL recorded."),
            ],
        ),
        "qa": _notion_ensure_page(
            required_sections["QA and Evidence"],
            f"{project_id.upper()} - QA Evidence",
            [_notion_paragraph(qa_summary or "QA summary pending.")],
        ),
        "retro": _notion_ensure_page(
            required_sections["Retrospectives"],
            f"{project_id.upper()} - Retrospective",
            [_notion_paragraph(retrospective_summary or "Retrospective pending.")],
        ),
    }
    if decision_summary:
        board_pages["decision"] = _notion_ensure_page(
            required_sections["Decisions"],
            f"DECISION - {project_id.upper()} Acceptance",
            [_notion_paragraph(decision_summary)],
        )

    return {
        "ok": bool(project_page.get("ok")),
        "template": "factory_project_standard_v1",
        "project": project_page,
        "project_children": project_children,
        "board_pages": board_pages,
        "metrics": {
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": int(duration_seconds or 0),
            "duration_human": duration if duration_seconds else "",
        },
    }


@mcp.tool()
def openclaw_factory_completion_notify(
    project_id: str,
    title: str,
    preview_url: str,
    repo_url: str = "",
    notion_url: str = "",
    status: str = "completed",
    duration_seconds: int = 0,
    recipient: str = "",
    summary: str = "",
    next_decision: str = "",
) -> dict[str, Any]:
    """Email Jean a concise Factory completion report using Zeus' configured mailbox."""
    sender = _env("EMAIL_ADDRESS")
    password = _env("EMAIL_PASSWORD")
    smtp_host = _env("EMAIL_SMTP_HOST", "smtp.gmail.com")
    try:
        smtp_port = int(_env("EMAIL_SMTP_PORT", "587") or "587")
    except ValueError:
        smtp_port = 587
    recipient = str(recipient or "").strip() or _env("FACTORY_COMPLETION_NOTIFY_TO") or _env("EMAIL_HOME_ADDRESS")
    if not sender or not password or not recipient:
        return {
            "ok": False,
            "error": "missing EMAIL_ADDRESS, EMAIL_PASSWORD, or recipient/EMAIL_HOME_ADDRESS",
            "sender_configured": bool(sender),
            "recipient_configured": bool(recipient),
        }

    project_id = str(project_id or "").strip()
    title = str(title or project_id or "Factory project").strip()
    status = str(status or "completed").strip()
    duration_line = (
        f"Duracion del ciclo: {_duration_text(duration_seconds)} ({int(duration_seconds)} segundos)\n"
        if duration_seconds
        else ""
    )
    body = "\n".join(
        line
        for line in [
            f"Jean, Zeus informa cierre de trabajo Factory: {title}",
            "",
            f"Proyecto: {project_id}" if project_id else "",
            f"Estado: {status}",
            duration_line.strip(),
            f"Preview: {preview_url}" if preview_url else "",
            f"Repo: {repo_url}" if repo_url else "",
            f"Notion: {notion_url}" if notion_url else "",
            "",
            "Resumen:",
            summary or "Trabajo Factory cerrado y listo para revision.",
            "",
            f"Siguiente decision: {next_decision}" if next_decision else "",
            "",
            "Enviado automaticamente por Zeus desde la infraestructura Sitio Uno GCP.",
        ]
        if line
    )
    msg = EmailMessage()
    msg["From"] = f"Zeus <{sender}>"
    msg["To"] = recipient
    msg["Subject"] = f"[Zeus/Factory] {title} - {status}"
    msg.set_content(body)

    started = time.monotonic()
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "recipient": recipient,
            "smtp_host": smtp_host,
            "duration_s": round(time.monotonic() - started, 3),
        }

    return {
        "ok": True,
        "recipient": recipient,
        "sender": sender,
        "subject": msg["Subject"],
        "duration_s": round(time.monotonic() - started, 3),
        "project_id": project_id,
        "preview_url": preview_url,
    }


@mcp.tool()
def openclaw_notion_runbook() -> dict[str, Any]:
    """Explain how Zeus should use Notion as Jean's human-readable operating mirror."""
    return {
        "ok": True,
        "role": "Notion is the human mirror, not the execution engine.",
        "write_when": [
            "A new initiative becomes a project or sprint candidate.",
            "Zeus delegates meaningful work to a branch or Factory role.",
            "A preview URL is published on the Preview Lab.",
            "A high-impact decision, risk, blocker, scope change, or acceptance result occurs.",
            "A sprint opens, closes, or needs a review summary.",
            "A branch report changes the strategic status Jean should see.",
        ],
        "do_not_write": [
            "Every low-level worker log line.",
            "Secrets, tokens, private credentials, or internal-only URLs.",
            "Speculative status without checking openclaw_branch_report or the relevant source.",
        ],
        "source_of_truth": {
            "execution": "branch receiver, local branch Kanban, repos, CI, Preview Lab runtime",
            "strategy": "Zeus memory, Notion operating board, Jean decisions",
            "cross_office_status": "openclaw_list_offices and openclaw_branch_report",
        },
        "preview_lab_rule": (
            "When Factory publishes a visible deliverable, Zeus records the /p/<project>/ URL, "
            "owner, acceptance criteria, known limitations, and Jean decision in Notion."
        ),
    }


@mcp.tool()
def openclaw_orchestration_workflow_definitions() -> dict[str, Any]:
    """List the durable workflow packs available to Zeus."""
    return _orchestration_api_request("GET", "/v1/workflow-definitions")


@mcp.tool()
def openclaw_orchestration_start_factory_project(
    title: str,
    objective: str,
    project_id: str,
    branch_id: str = "sicilia",
    complexity: str = "standard",
    autonomy_level: str = "L2",
    sprint_goal: str = "",
    backlog_json: str = "",
) -> dict[str, Any]:
    """Low-level workflow creation only; user Factory jobs should use openclaw_factory_project_request.

    This tool creates durable state, steps, and work orders. It does not perform
    the canonical Factory handoff by itself. For Jean/user software requests,
    call openclaw_factory_project_request so Zeus creates the workflow and
    delegates through Sicilia/Leo in one governed operation.
    """
    title = str(title or "").strip()
    objective = str(objective or "").strip()
    project_id = str(project_id or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    if not objective:
        return {"ok": False, "error": "objective is required"}
    if not project_id:
        return {"ok": False, "error": "project_id is required"}
    backlog_items: list[dict[str, Any]] = []
    if str(backlog_json or "").strip():
        try:
            parsed = json.loads(backlog_json)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid backlog_json: {exc}"}
        if not isinstance(parsed, list):
            return {"ok": False, "error": "backlog_json must be a JSON array"}
        backlog_items = [item for item in parsed if isinstance(item, dict)]
    payload = {
        "title": title,
        "objective": objective,
        "created_by": "zeus",
        "project_id": project_id,
        "branch_id": str(branch_id or "sicilia").strip().lower(),
        "complexity": str(complexity or "standard").strip().lower(),
        "autonomy_level": str(autonomy_level or "L2").strip().upper(),
        "sprint_goal": str(sprint_goal or "").strip(),
        "backlog_items": backlog_items,
    }
    result = _orchestration_api_request(
        "POST",
        "/v1/workflows/factory-scrum",
        payload=payload,
        timeout_s=20.0,
    )
    if result.get("ok"):
        result["canonical_note"] = (
            "Workflow was created only. For user-facing Factory work, the "
            "canonical entrypoint is openclaw_factory_project_request because "
            "it also creates the governed delegation envelope."
        )
        result["next_required_tool"] = "openclaw_factory_project_request"
    return result


@mcp.tool()
def openclaw_orchestration_status(workflow_run_id: str) -> dict[str, Any]:
    """Read the canonical status, steps, work orders, timeline and Kanban projection for one workflow."""
    workflow_run_id = str(workflow_run_id or "").strip()
    if not workflow_run_id:
        return {"ok": False, "error": "workflow_run_id is required"}
    encoded = quote(workflow_run_id)
    run = _orchestration_api_request("GET", f"/v1/workflow-runs/{encoded}")
    steps = _orchestration_api_request("GET", f"/v1/workflow-runs/{encoded}/steps")
    work_orders = _orchestration_api_request("GET", f"/v1/workflow-runs/{encoded}/work-orders")
    kanban = _orchestration_api_request("GET", f"/v1/workflow-runs/{encoded}/kanban")
    timeline = _orchestration_api_request("GET", f"/v1/workflow-runs/{encoded}/timeline")
    return {
        "ok": all(item.get("ok") for item in (run, steps, work_orders, kanban, timeline)),
        "workflow_run_id": workflow_run_id,
        "run": run,
        "steps": steps,
        "work_orders": work_orders,
        "kanban": kanban,
        "timeline": timeline,
    }


@mcp.tool()
def openclaw_orchestration_kanban(workflow_run_id: str) -> dict[str, Any]:
    """Read the derived Kanban projection for one workflow. This is a view, not the source of truth."""
    workflow_run_id = str(workflow_run_id or "").strip()
    if not workflow_run_id:
        return {"ok": False, "error": "workflow_run_id is required"}
    return _orchestration_api_request(
        "GET",
        f"/v1/workflow-runs/{quote(workflow_run_id)}/kanban",
    )


@mcp.tool()
def openclaw_orchestration_watchdog(actor: str = "zeus-watchdog") -> dict[str, Any]:
    """Run active supervision: mark stale work orders timed out and request Zeus intervention."""
    return _orchestration_api_request(
        "POST",
        "/v1/watchdog/run",
        payload={"actor": str(actor or "zeus-watchdog").strip()},
        timeout_s=20.0,
    )


@mcp.tool()
def openclaw_orchestration_intervention(
    workflow_run_id: str,
    reason: str,
    action: str = "inspect",
    work_order_id: str = "",
) -> dict[str, Any]:
    """Record a required Zeus intervention against a workflow or work order."""
    workflow_run_id = str(workflow_run_id or "").strip()
    if not workflow_run_id:
        return {"ok": False, "error": "workflow_run_id is required"}
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "error": "reason is required"}
    payload = {
        "actor": "zeus",
        "reason": reason,
        "action": str(action or "inspect").strip(),
        "work_order_id": str(work_order_id or "").strip() or None,
    }
    return _orchestration_api_request(
        "POST",
        f"/v1/workflow-runs/{quote(workflow_run_id)}/interventions",
        payload={key: value for key, value in payload.items() if value is not None},
        timeout_s=20.0,
    )


@mcp.tool()
def openclaw_orchestration_resolve_intervention(
    workflow_run_id: str,
    reason: str,
    outcome: str = "resolved",
    work_order_id: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Resolve an active Zeus intervention once the blocking cause has been corrected."""
    workflow_run_id = str(workflow_run_id or "").strip()
    if not workflow_run_id:
        return {"ok": False, "error": "workflow_run_id is required"}
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "error": "reason is required"}
    payload = {
        "actor": "zeus",
        "reason": reason,
        "outcome": str(outcome or "resolved").strip(),
        "work_order_id": str(work_order_id or "").strip() or None,
        "notes": str(notes or "").strip(),
    }
    return _orchestration_api_request(
        "POST",
        f"/v1/workflow-runs/{quote(workflow_run_id)}/interventions/resolve",
        payload={key: value for key, value in payload.items() if value is not None},
        timeout_s=20.0,
    )


@mcp.tool()
def openclaw_orchestration_cancel_work_order(
    work_order_id: str,
    reason: str,
    notes: str = "",
) -> dict[str, Any]:
    """Cancel an obsolete or superseded work order through the orchestration API."""
    work_order_id = str(work_order_id or "").strip()
    if not work_order_id:
        return {"ok": False, "error": "work_order_id is required"}
    reason = str(reason or "").strip()
    if not reason:
        return {"ok": False, "error": "reason is required"}
    return _orchestration_api_request(
        "POST",
        f"/v1/work-orders/{quote(work_order_id)}/cancel",
        payload={
            "actor": "zeus",
            "reason": reason,
            "notes": str(notes or "").strip(),
        },
        timeout_s=20.0,
    )


@mcp.tool()
def openclaw_orchestration_complete_workflow(
    workflow_run_id: str,
    summary: str,
    notes: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Mark a workflow run completed after all work orders are completed or cancelled."""
    workflow_run_id = str(workflow_run_id or "").strip()
    if not workflow_run_id:
        return {"ok": False, "error": "workflow_run_id is required"}
    summary = str(summary or "").strip()
    if not summary:
        return {"ok": False, "error": "summary is required"}
    return _orchestration_api_request(
        "POST",
        f"/v1/workflow-runs/{quote(workflow_run_id)}/complete",
        payload={
            "actor": "zeus",
            "summary": summary,
            "notes": str(notes or "").strip(),
            "force": bool(force),
        },
        timeout_s=20.0,
    )


@mcp.tool()
def openclaw_factory_project_request(
    title: str,
    request: str,
    project_slug: str = "",
    project_id: str = "",
    complexity: str = "simple",
    autonomy_level: str = "L2",
    deadline_s: int = 300,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Submit software/product development work through the canonical Factory route.

    This is the preferred tool for web, app, backend, design-to-code, preview,
    repo, QA, or software-delivery work. It creates a durable workflow run in
    Hermes Orchestration Core before delegating asynchronously to Leo.
    """
    title = str(title or "").strip()
    request = str(request or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    if not request:
        return {"ok": False, "error": "request is required"}

    pid, slug, repo_name = _factory_project_id(title, project_slug=project_slug, project_id=project_id)
    deadline = max(30, min(int(deadline_s or 300), MAX_DEADLINE_S))
    complexity = str(complexity or "simple").strip().lower()
    if complexity not in {"simple", "standard", "complex"}:
        complexity = "standard"
    autonomy_level = str(autonomy_level or "L2").strip().upper()
    preview_url = f"https://kidu.app/p/{slug}/"
    repo_preflight: dict[str, Any] = {
        "ok": True,
        "dry_run": True,
        "action": "would_ensure_private_repo",
        "repo_name": repo_name,
        "repo_url": f"https://github.com/SiteOneTech/{repo_name}",
        "clone_url": f"https://github.com/SiteOneTech/{repo_name}.git",
        "visibility": "private",
        "default_branch": "main",
    }
    metadata = {
        "canonical_factory_project": True,
        "source": "zeus_factory_project_request",
        "project_id": pid,
        "project_slug": slug,
        "normalized_from": {
            "title": title,
            "project_slug": project_slug,
            "project_id": project_id,
        },
        "repo_name": repo_name,
        "repo_visibility": "private",
        "preview_url_expected": preview_url,
        "notion_required": True,
        "notion_template": "factory_project_standard_v1",
        "completion_email_required": True,
        "playwright_required": True,
        "footer_credit_required": "desarrollado por: SitioUno Factory",
        "complexity": complexity,
        "autonomy_level": autonomy_level,
        "role_boundary_enforced": True,
        "factory_feedback_channel": "openclaw_branch_report.factory_feedback",
        "stage_order": [stage[1] for stage in FACTORY_CANONICAL_STAGES],
    }

    backlog_items = _factory_backlog_items(pid, title, complexity)
    orchestration_request = {
        "title": title,
        "objective": request,
        "created_by": "zeus",
        "project_id": pid,
        "branch_id": "sicilia",
        "complexity": complexity,
        "autonomy_level": autonomy_level,
        "sprint_goal": f"Deliver the first governed increment for {title}",
        "backlog_items": backlog_items,
    }

    def _task_spec(
        workflow_run_id: str = "pending",
        sprint_id: str = "sprint-001",
        work_orders: list[dict[str, Any]] | None = None,
    ) -> str:
        repo_url = str(repo_preflight.get("repo_url") or f"https://github.com/SiteOneTech/{repo_name}")
        clone_url = str(repo_preflight.get("clone_url") or f"{repo_url}.git")
        default_branch = str(repo_preflight.get("default_branch") or "main")
        branch_prefix = f"factory/{slug}"
        work_order_lines = []
        for item in work_orders or []:
            work_order_lines.append(
                "- {work_order_id} [{owner_role}] {task}".format(
                    work_order_id=item.get("work_order_id", ""),
                    owner_role=item.get("owner_role", ""),
                    task=str(item.get("task", ""))[:180],
                )
            )
        lines = [
            "TASKSPEC_CANONICAL_FACTORY_PROJECT",
            f"Project ID: {pid}",
            f"Project slug: {slug}",
            f"Workflow run ID: {workflow_run_id}",
            f"Sprint ID: {sprint_id}",
            f"Title: {title}",
            f"Complexity: {complexity}",
            f"Autonomy level: {autonomy_level}",
            f"Required private repo: {repo_url}",
            f"Git clone URL: {clone_url}",
            f"Default branch: {default_branch}",
            f"Required work branch prefix: {branch_prefix}",
            f"Required preview target: {preview_url}",
            "Required footer credit on every public preview page: desarrollado por: SitioUno Factory",
            "",
            "Jean's request:",
            request,
            "",
            "Mandatory route:",
            "1. Do not satisfy this by creating a loose file under /home or by only answering in chat.",
            "2. Treat Hermes Orchestration Core as the source of truth for status, gates, retries, timeouts and work-order ownership.",
            "3. Treat every Kanban board as a derived visibility projection; do not hand-edit it as execution state.",
            "4. Use the workflow_run_id and matching work_order_id in every artifact, callback, branch report and observability log.",
            "5. Follow IDEA -> DISCOVERY -> PRODUCT_SHAPING -> ARCHITECTURE_REVIEW -> READY_FOR_SPRINT -> EXECUTION -> CODE_REVIEW -> QA_VALIDATION -> SECURITY_REVIEW -> ZEUS_ACCEPTANCE -> RELEASE -> RETROSPECTIVE -> MEMORY_UPDATE.",
            "6. Zeus has already ensured the private GitHub repo before this delegation; do not create a competing repo unless Zeus records a blocker and changes the repo contract.",
            "7. Use the node's configured GitHub credential (GH_TOKEN/GITHUB_TOKEN/gh auth) for clone, commit and push; never print, request, or store the token in artifacts, logs, Markdown, Notion, or chat.",
            "8. Work in a branch under the required prefix and push every code-bearing deliverable to the repo.",
            "9. Every coding/execution callback must include repo_url, branch, commit_sha, changed_files and artifact/log refs.",
            "10. Produce project docs/evidence: PRD, architecture brief, sprint plan, execution log, code review, QA report, Browser QA/Playwright report, security review, release handoff, retrospective, metrics.",
            "11. Publish user-facing work to the KIDU Preview Lab unless Zeus/Jean explicitly says not to publish.",
            "12. Record Notion human-readable evidence through Zeus or the approved writer.",
            "13. Metrics must record started_at, completed_at, total duration, duration by state when available, engine attempts, QA result, Playwright result, re-gate count, and preview deploy time.",
            "14. When the work is complete enough for Jean review, Zeus must send Jean an email completion report with status, preview URL, repo URL, Notion URL, duration, and next decision.",
            "15. If any QA/security/release gate fails, mark REGATE_REQUIRED and do not present the work as accepted.",
            "16. Report progress asynchronously; Zeus will inspect orchestration status, watchdog events and delegation status.",
            "17. Leo Orquestador must orchestrate only: create role-owned work orders, route engines, record blockers, and assign the next owner.",
            "18. Leo must not write or close specialist deliverables for Vera, Mia, Nico, Iris, Bruno, Tina, Belen, Sofia, Rene, or Dario.",
            "19. Every successful stage must include the responsible owner, matching work_order_id, artifact paths, and gate result in FactoryRun.",
            "20. Exit code 0 or a good narrative is not completion; success without owner evidence must be marked partial/blocked.",
            "21. Execution engines must be explicit in every code-bearing work order: codex, claude_code, openhands_vm, or openhands_cli.",
            "22. For OpenHands production execution use engine_id=openhands_vm through the openhands_runner VM connector. openhands_cli is only an explicitly selected fallback/benchmark path and must never silently replace the VM.",
            "23. OpenHands VM uses its UI-configured GitHub integration for private repos; never pass GitHub tokens in prompts, artifacts, logs, Markdown, Notion, or chat.",
            "24. Every execution callback must include engine_id, execution_surface, runner_task_id or branch_task_id, attempt number, duration, error taxonomy, re-gate count, repo_url, branch, commit_sha and changed_files.",
        ]
        if work_order_lines:
            lines.extend(["", "Canonical work orders:", *work_order_lines])
        return "\n".join(lines)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "project_id": pid,
            "project_slug": slug,
            "repo_name": repo_name,
            "repo_preflight": repo_preflight,
            "preview_url_expected": preview_url,
            "orchestration_request": orchestration_request,
            "zeus_kanban_task_id": f"zeus-{pid}",
            "delegation_task_spec": _task_spec(),
        }

    orchestration = _orchestration_api_request(
        "POST",
        "/v1/workflows/factory-scrum",
        payload=orchestration_request,
        timeout_s=20.0,
    )
    if not orchestration.get("ok"):
        return {
            "ok": False,
            "error": "Hermes Orchestration Core rejected the Factory workflow; refusing to delegate untracked work",
            "project_id": pid,
            "project_slug": slug,
            "orchestration": orchestration,
        }

    orchestration_body = orchestration.get("result") if isinstance(orchestration.get("result"), dict) else {}
    workflow_run = orchestration_body.get("workflow_run") if isinstance(orchestration_body, dict) else {}
    sprint = orchestration_body.get("sprint") if isinstance(orchestration_body, dict) else {}
    if not isinstance(workflow_run, dict):
        workflow_run = {}
    if not isinstance(sprint, dict):
        sprint = {}
    workflow_run_id = str(workflow_run.get("workflow_run_id") or "")
    sprint_id = str(sprint.get("sprint_id") or "sprint-001")
    work_orders = sprint.get("work_orders") if isinstance(sprint.get("work_orders"), list) else []
    repo_preflight = _ensure_factory_github_repo(repo_name, title, slug)
    if not repo_preflight.get("ok"):
        intervention = None
        if workflow_run_id:
            intervention = _orchestration_api_request(
                "POST",
                f"/v1/workflow-runs/{quote(workflow_run_id)}/interventions",
                payload={
                    "actor": "zeus",
                    "reason": (
                        "GitHub repo preflight failed before Factory delegation: "
                        f"{repo_preflight.get('error')}"
                    ),
                    "action": "blocked",
                    "notes": (
                        "The Factory workflow was created, but Sicilia delegation was "
                        "refused because workers would not have a valid repository."
                    ),
                },
                timeout_s=20.0,
            )
        zeus_kanban = _zeus_kanban_upsert(
            task_id=f"zeus-{pid}",
            title=f"Factory {title}",
            body=(
                f"Factory project {pid} is blocked before delegation. "
                f"Expected repo: {repo_name}. GitHub preflight failed: "
                f"{repo_preflight.get('error')}. Hermes workflow_run_id: {workflow_run_id}."
            ),
            status="blocked",
            priority=1,
            result={
                "project_id": pid,
                "repo_name": repo_name,
                "canonical_factory_project": True,
                "workflow_run_id": workflow_run_id,
                "repo_preflight": repo_preflight,
            },
        )
        return {
            "ok": False,
            "error": "GitHub repo preflight failed; refusing to delegate untracked Factory work",
            "project_id": pid,
            "project_slug": slug,
            "repo_name": repo_name,
            "repo_preflight": repo_preflight,
            "canonical_state": "hermes_orchestration_core",
            "workflow_run_id": workflow_run_id,
            "sprint_id": sprint_id,
            "orchestration": orchestration,
            "zeus_kanban": zeus_kanban,
            "intervention": intervention,
        }
    initial_work_order_id = _factory_initial_delegation_work_order_id(work_orders)
    task_spec = _task_spec(workflow_run_id or "unknown", sprint_id, work_orders)
    metadata = {
        **metadata,
        "repo_preflight": repo_preflight,
        "repo_url": repo_preflight.get("repo_url"),
        "clone_url": repo_preflight.get("clone_url"),
        "default_branch": repo_preflight.get("default_branch"),
        "workflow_run_id": workflow_run_id,
        "work_order_id": initial_work_order_id,
        "delegation_context": {
            "target_office_id": "sicilia",
            "target_agent_id": "leo-orquestador",
            "initial_work_order_id": initial_work_order_id,
            "sprint_id": sprint_id,
        },
        "orchestration": {
            "api_url": _orchestration_base_url(),
            "workflow_run_id": workflow_run_id,
            "sprint_id": sprint_id,
            "initial_work_order_id": initial_work_order_id,
            "kanban_projection_path": f"/v1/workflow-runs/{workflow_run_id}/kanban",
            "work_order_ids": [
                item.get("work_order_id")
                for item in work_orders
                if isinstance(item, dict) and item.get("work_order_id")
            ],
        },
    }

    notion_event = openclaw_notion_log_event(
        title=f"{pid} submitted to Factory",
        summary=f"{title}\n\nExpected repo: {repo_name}\nExpected preview: {preview_url}",
        category="factory",
        status="submitted",
        preview_url=preview_url,
    )
    zeus_kanban = _zeus_kanban_upsert(
        task_id=f"zeus-{pid}",
        title=f"Factory {title}",
        body=(
            f"Strategic oversight for Factory project {pid}. "
            f"Branch: sicilia. Expected repo: {repo_name}. Expected preview: {preview_url}. "
            f"Hermes workflow_run_id: {workflow_run_id}. "
            "Hermes Orchestration Core is the execution truth; Kanban is only a visibility mirror. "
            "Use openclaw_orchestration_status and openclaw_orchestration_watchdog for supervision."
        ),
        status="running",
        priority=2 if complexity in {"standard", "complex"} else 3,
        result={
            "project_id": pid,
            "branch": "sicilia",
            "repo_name": repo_name,
            "repo_preflight": repo_preflight,
            "preview_url_expected": preview_url,
            "canonical_factory_project": True,
            "workflow_run_id": workflow_run_id,
            "orchestration_kanban_projection": f"/v1/workflow-runs/{workflow_run_id}/kanban",
        },
    )
    delegation = openclaw_delegate_task(
        office_id="sicilia",
        agent_id="leo-orquestador",
        task=task_spec,
        deadline_s=deadline,
        dry_run=False,
        async_mode=True,
        project_id=pid,
        initiative_id=pid,
        metadata_json=json.dumps(metadata, ensure_ascii=True, sort_keys=True),
    )
    intervention = None
    if not delegation.get("ok") and workflow_run_id:
        intervention = _orchestration_api_request(
            "POST",
            f"/v1/workflow-runs/{quote(workflow_run_id)}/interventions",
            payload={
                "actor": "zeus",
                "reason": f"Delegation to Sicilia failed: {delegation.get('error')}",
                "action": "blocked",
            },
            timeout_s=20.0,
        )
    return {
        "ok": bool(delegation.get("ok")),
        "project_id": pid,
        "project_slug": slug,
        "repo_name": repo_name,
        "repo_preflight": repo_preflight,
        "preview_url_expected": preview_url,
        "canonical_state": "hermes_orchestration_core",
        "workflow_run_id": workflow_run_id,
        "sprint_id": sprint_id,
        "orchestration": orchestration,
        "notion_event": notion_event,
        "zeus_kanban": zeus_kanban,
        "delegation": delegation,
        "intervention": intervention,
        "next_poll": {
            "workflow": {
                "tool": "openclaw_orchestration_status",
                "workflow_run_id": workflow_run_id,
            },
            "watchdog": {"tool": "openclaw_orchestration_watchdog"},
            "delegation": {
                "tool": "openclaw_delegation_status",
                "office_id": "sicilia",
                "task_id": delegation.get("task_id"),
            },
        },
    }


@mcp.tool()
def openclaw_openhands_delegate_task(
    task: str,
    title: str = "",
    repository: str = "",
    branch: str = "",
    route: str = "factory",
    execute: bool = False,
    async_mode: bool = True,
    deadline_s: int = 300,
) -> dict[str, Any]:
    """Route a development task to OpenHands without bypassing Factory governance.

    Default route is `factory`: Zeus delegates to Sicilia's `olga-openhands`
    agent, which owns engine routing. Use `route="vm"` or `route="runner"` for
    an approved OpenHands runner VM task envelope. Set execute=false to validate
    the route without starting work. Direct connector execution is disabled by
    default and must not be used for normal Factory work.
    """
    task = str(task or "").strip()
    if not task:
        return {"ok": False, "error": "task is required"}
    title = str(title or "OpenHands delegated task").strip()
    repository = str(repository or "").strip()
    branch = str(branch or "").strip()
    route = str(route or "factory").strip().lower()
    deadline = max(5, min(int(deadline_s or 300), MAX_DEADLINE_S))

    governed_task = "\n".join(
        part
        for part in [
            f"OpenHands execution request: {title}",
            f"Repository: {repository}" if repository else "",
            f"Branch: {branch}" if branch else "",
            "",
            "Use OpenHands only if it is the right execution path for this development task.",
            "If OpenHands execution is required, use engine_id=openhands_vm through the openhands_runner VM connector.",
            "Use engine_id=openhands_cli only as an explicitly selected fallback/benchmark and record it separately.",
            "Never print, request, or store GitHub tokens; the VM uses its UI-configured GitHub integration.",
            "Keep Zeus as strategic supervisor; do not assign Zeus implementation tickets.",
            "",
            task,
        ]
        if part != ""
    )

    if route == "factory":
        if not execute:
            return openclaw_delegate_task(
                office_id="sicilia",
                agent_id="olga-openhands",
                task=governed_task,
                deadline_s=deadline,
                dry_run=True,
                async_mode=async_mode,
            )
        return openclaw_delegate_task(
            office_id="sicilia",
            agent_id="olga-openhands",
            task=governed_task,
            deadline_s=deadline,
            dry_run=False,
            async_mode=async_mode,
        )

    if route in {"vm", "runner", "openhands_vm"}:
        office, err = _openhands_runner()
        if err:
            return err
        assert office is not None
        connector_url = _openhands_connector_url(office)
        token_env, token = _openhands_connector_token(office)
        if not connector_url:
            return {"ok": False, "error": "OpenHands connector URL is not configured"}
        if not token:
            return {
                "ok": False,
                "error": f"missing OpenHands connector token env {token_env}",
                "token_env": token_env,
            }

        payload = {
            "title": title,
            "task": governed_task,
            "repository": repository,
            "branch": branch,
            "dry_run": not execute,
            "git_provider": "github" if repository else "",
            "system_message_suffix": (
                "Factory execution surface: openhands_vm. Use the OpenHands "
                "runner VM and its configured GitHub integration. Do not ask "
                "for, print, or persist GitHub tokens. Return repo_url, branch, "
                "commit_sha, changed_files, verification, duration, error "
                "taxonomy, and runner log reference."
            ),
        }
        result = _http_json_request(
            "POST",
            urljoin(f"{connector_url}/", "v1/openhands/tasks"),
            payload=payload,
            bearer_token=token,
            timeout_s=deadline,
        )
        runner_task_id = _openhands_runner_task_id(result)
        response_body = result.get("body_json") if isinstance(result.get("body_json"), dict) else {}
        result_payload: dict[str, Any] = {
            "ok": bool(result.get("ok")),
            "route": "vm",
            "engine_id": "openhands_vm",
            "execution_surface": "openhands_runner_vm",
            "runner": "openhands_runner",
            "connector_url": connector_url,
            "dry_run": not execute,
            "token_env": token_env,
            "token_configured": bool(token),
            "runner_task_id": runner_task_id or None,
            "result": result,
        }
        if runner_task_id:
            result_payload["task_status"] = {
                "tool": "openclaw_openhands_task_status",
                "task_id": runner_task_id,
            }
        if result.get("ok"):
            result_payload["orchestration_sync"] = _sync_orchestration_delegation_acceptance(
                task_text=governed_task,
                request_payload=payload,
                office_id="openhands_runner",
                agent_id="openhands_vm",
                branch_task_id=runner_task_id or "unknown",
                async_mode=True,
                response=response_body,
                transport="openhands-runner-v1",
                heartbeat_notes="OpenHands runner VM accepted delegated work.",
            )
        else:
            result_payload["orchestration_sync"] = _sync_orchestration_delegation_failure(
                task_text=governed_task,
                request_payload=payload,
                office_id="openhands_runner",
                agent_id="openhands_vm",
                branch_task_id=runner_task_id or "unknown",
                error=str(result.get("error") or response_body)[:1000],
            )
        return result_payload

    if route != "direct":
        return {
            "ok": False,
            "error": "route must be 'factory', 'vm', 'runner' or 'direct'",
            "allowed_routes": ["factory", "vm", "runner", "direct"],
        }

    direct_enabled = _env("OPENCLAW_ALLOW_DIRECT_OPENHANDS_EXECUTION").lower() in {
        "1",
        "true",
        "yes",
    }
    if execute and not direct_enabled:
        return {
            "ok": False,
            "route": "direct",
            "dry_run": False,
            "direct_execution_enabled": False,
            "error": (
                "Direct OpenHands execution is disabled. Use route='factory' "
                "with execute=true so Sicilia/olga-openhands owns routing, "
                "or route='vm' so the dedicated OpenHands runner VM owns execution, "
                "or set OPENCLAW_ALLOW_DIRECT_OPENHANDS_EXECUTION=1 only for "
                "an explicitly approved break-glass task."
            ),
        }

    office, err = _openhands_runner()
    if err:
        return err
    assert office is not None
    connector_url = _openhands_connector_url(office)
    token_env, token = _openhands_connector_token(office)
    if not connector_url:
        return {"ok": False, "error": "OpenHands connector URL is not configured"}
    if not token:
        return {"ok": False, "error": f"missing OpenHands connector token env {token_env}", "token_env": token_env}

    payload = {
        "title": title,
        "task": task,
        "repository": repository,
        "branch": branch,
        "dry_run": not execute,
    }
    result = _http_json_request(
        "POST",
        urljoin(f"{connector_url}/", "v1/openhands/tasks"),
        payload=payload,
        bearer_token=token,
        timeout_s=deadline,
    )
    return {
        "ok": bool(result.get("ok")),
        "route": "direct",
        "connector_url": connector_url,
        "dry_run": not execute,
        "token_env": token_env,
        "token_configured": bool(token),
        "result": result,
    }


@mcp.tool()
def openclaw_tailnet_status() -> dict[str, Any]:
    """Return the live Tailscale peers visible from the Hermes VM."""
    return _tailscale_status()


@mcp.tool()
def openclaw_delegate_task(
    office_id: str,
    agent_id: str,
    task: str,
    deadline_s: int = 120,
    max_output_bytes: int = 262144,
    dry_run: bool = False,
    async_mode: bool = True,
    project_id: str = "",
    initiative_id: str = "",
    metadata_json: str = "",
) -> dict[str, Any]:
    """Delegate a task to an OpenClaw office agent using branch-delegation-v1.

    Set dry_run=true to validate routing and show the request envelope without
    contacting the remote office. By default real delegations are async: the
    office accepts quickly and Zeus monitors progress through
    openclaw_delegation_status or openclaw_branch_report.
    """
    office_id = str(office_id or "").strip().lower()
    agent_id = str(agent_id or "").strip().lower()
    task = str(task or "").strip()
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None

    agents = office.get("agents") or {}
    if agents and agent_id not in agents:
        return {
            "ok": False,
            "error": f"Agent '{agent_id}' is not registered for office '{office_id}'",
            "known_agents": sorted(agents),
        }
    if not task:
        return {"ok": False, "error": "task is required"}

    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token and not dry_run:
        return {
            "ok": False,
            "error": f"missing delegation token env {token_env}",
            "token_env": token_env,
        }

    deadline = max(5, min(int(deadline_s or 120), MAX_DEADLINE_S))
    task_id = str(uuid.uuid4())
    payload = {
        "version": "1",
        "task_id": task_id,
        "idempotency_key": task_id,
        "target": {"branch": office_id, "agent_id": agent_id},
        "input": task,
        "deadline_s": deadline,
        "max_output_bytes": int(max_output_bytes or 262144),
    }
    if str(project_id or "").strip():
        payload["project_id"] = str(project_id).strip()
    if str(initiative_id or "").strip():
        payload["initiative_id"] = str(initiative_id).strip()
    parsed_metadata: dict[str, Any] = {}
    if str(metadata_json or "").strip():
        try:
            parsed_metadata = json.loads(str(metadata_json))
        except json.JSONDecodeError:
            parsed_metadata = {"raw": str(metadata_json)[:2000]}
        if isinstance(parsed_metadata, dict):
            payload["metadata"] = parsed_metadata
    if async_mode:
        payload["async"] = True
        payload["mode"] = "async"

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "async_mode": async_mode,
            "endpoint": endpoint,
            "token_env": token_env,
            "token_configured": bool(token),
            "request": payload,
            "orchestration_reference": {
                "workflow_run_id": _orchestration_refs_from_task(task, payload)[0],
                "work_order_id": _orchestration_refs_from_task(task, payload)[1],
            },
        }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hermes-openclaw-office/1.0",
        },
    )
    started = time.monotonic()
    timeout_s = min(20.0, float(deadline + DEFAULT_TIMEOUT_S)) if async_mode else float(deadline + DEFAULT_TIMEOUT_S)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(max(1024, int(max_output_bytes or 262144)) + 4096)
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"raw": text}
            result_payload = {
                "ok": True,
                "http_status": resp.status,
                "duration_s": round(time.monotonic() - started, 3),
                "task_id": task_id,
                "async_mode": async_mode,
                "response": parsed,
            }
            result_payload["orchestration_sync"] = _sync_orchestration_delegation_acceptance(
                task_text=task,
                request_payload=payload,
                office_id=office_id,
                agent_id=agent_id,
                branch_task_id=task_id,
                async_mode=async_mode,
                response=parsed,
            )
            return result_payload
    except urllib.error.HTTPError as exc:
        text = exc.read(4096).decode("utf-8", errors="replace")
        result_payload = {
            "ok": False,
            "http_status": exc.code,
            "duration_s": round(time.monotonic() - started, 3),
            "task_id": task_id,
            "async_mode": async_mode,
            "error": text or str(exc),
        }
        result_payload["orchestration_sync"] = _sync_orchestration_delegation_failure(
            task_text=task,
            request_payload=payload,
            office_id=office_id,
            agent_id=agent_id,
            branch_task_id=task_id,
            error=text or str(exc),
        )
        return result_payload
    except Exception as exc:
        result_payload = {
            "ok": False,
            "duration_s": round(time.monotonic() - started, 3),
            "task_id": task_id,
            "async_mode": async_mode,
            "error": str(exc),
        }
        result_payload["orchestration_sync"] = _sync_orchestration_delegation_failure(
            task_text=task,
            request_payload=payload,
            office_id=office_id,
            agent_id=agent_id,
            branch_task_id=task_id,
            error=str(exc),
        )
        return result_payload


@mcp.tool()
def openclaw_delegation_status(office_id: str, task_id: str) -> dict[str, Any]:
    """Poll one delegated task by task_id from a branch receiver."""
    office_id = str(office_id or "").strip().lower()
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    office, err = _office_or_error(office_id)
    if err:
        return err
    assert office is not None
    endpoint = str(office.get("delegate_endpoint") or "").strip()
    token_env = str(office.get("token_env") or "").strip()
    token = _env(token_env) if token_env else ""
    if not endpoint:
        return {"ok": False, "error": f"office '{office_id}' has no delegate_endpoint"}
    if not token:
        return {"ok": False, "error": f"missing delegation token env {token_env}", "token_env": token_env}
    url = f"{endpoint.rstrip('/')}/tasks/{quote(task_id)}"
    result = _http_json_request("GET", url, bearer_token=token, timeout_s=10.0)
    return {
        "ok": bool(result.get("ok")),
        "office_id": office_id,
        "task_id": task_id,
        "endpoint": url,
        "http_status": result.get("status"),
        "duration_s": result.get("duration_s"),
        "status": result.get("body_json"),
        "error": result.get("error"),
    }


@mcp.tool()
def openclaw_delegation_runbook() -> dict[str, Any]:
    """Explain the recommended architecture for adding offices and bots to Hermes."""
    return {
        "ok": True,
        "steps": [
            "Use the HQ registry (/v1/branches) as the live inventory for branch agents and heartbeats.",
            "Register each office in openclaw-fleet.yaml with stable node id, tailnet host/IP, endpoint, and token env.",
            "For approved nodes, let HQ broker the delegation token to Zeus through /v1/zeus/delegation-tokens; do not paste tokens manually.",
            "Expose each office through the branch-delegation-v1 receiver or an approved gateway/queue adapter.",
            "Store each office token only in ~/.hermes/.env as OPENCLAW_<OFFICE>_DELEGATE_TOKEN.",
            "Enable the openclaw-office MCP server in Hermes for CLI and Telegram toolsets.",
            "For OpenHands work, prefer explicit engine routing: route='factory' for governed Sicilia ownership, route='vm' for approved OpenHands runner VM execution, and route='direct' only for break-glass diagnostics.",
            "Use async delegation for non-trivial work: submit with async_mode=true, then poll openclaw_delegation_status and openclaw_branch_report instead of waiting for a long HTTP response.",
            "Before delegating, call openclaw_office_status to verify tailnet, endpoint, token, and agent registration.",
            "For new offices, add the node to Tailscale, deploy a receiver, add a token, register agents, then test with dry_run before live delegation.",
        ],
        "notes": [
            "Tailscale connectivity lets nodes reach each other; it does not define what commands are allowed.",
            "The delegation receiver should run an allowlisted OpenClaw agent command, not arbitrary shell.",
            "For bots, prefer platform-specific gateways or send_message targets; for agents, prefer MCP/HTTP delegation.",
        ],
    }


def _self_test() -> None:
    print(json.dumps(openclaw_list_offices(include_live=True), indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return
    mcp.run("stdio")


if __name__ == "__main__":
    main()

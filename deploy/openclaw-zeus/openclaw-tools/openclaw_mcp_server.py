#!/usr/bin/env python3
"""MCP tools that let Hermes understand and delegate to OpenClaw offices.

The server is intentionally small and conservative:
- Inventory lives in a YAML file with no secrets.
- Delegation tokens are read from environment variables only.
- The remote contract is the documented branch-delegation-v1 HTTP API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

try:
    import yaml
except Exception:  # pragma: no cover - Hermes ships PyYAML, fallback is for diagnostics.
    yaml = None

from mcp.server.fastmcp import FastMCP


DEFAULT_CONFIG = Path.home() / ".hermes" / "openclaw-tools" / "openclaw-fleet.yaml"
DEFAULT_NOTION_STATE = Path.home() / ".hermes" / "openclaw-tools" / "notion-state.json"
MAX_DEADLINE_S = 600
DEFAULT_TIMEOUT_S = 8
DEFAULT_REGISTRY_API_URL = "http://openclaw-hq:8781"
DEFAULT_NOTION_VERSION = "2022-06-28"
FACTORY_CANONICAL_STAGES = (
    ("idea", "IDEA", "leo-orquestador", "done"),
    ("discovery", "DISCOVERY", "vera-research", "ready"),
    ("product-shaping", "PRODUCT_SHAPING", "mia-producto", "backlog"),
    ("architecture-review", "ARCHITECTURE_REVIEW", "nico-arquitecto", "backlog"),
    ("ready-for-sprint", "READY_FOR_SPRINT", "ana-pmo", "backlog"),
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
    dotenv = hermes_home / ".env"
    if not dotenv.exists():
        return
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


def _factory_project_id(title: str, project_slug: str = "", project_id: str = "") -> tuple[str, str, str]:
    slug = _slugify(project_slug or title, fallback="factory-project")
    pid = _slugify(project_id, fallback="") if project_id else f"factory-{slug}-001"
    repo_name = f"factory-su-{slug}"
    return pid, slug, repo_name


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
    return {
        "ok": bool(result.get("ok") and result.get("status") == 200 and isinstance(report, dict)),
        "office_id": office_id,
        "endpoint": endpoint,
        "http_status": result.get("status"),
        "duration_s": result.get("duration_s"),
        "report": report if isinstance(report, dict) else None,
        "probe": result if not isinstance(report, dict) else None,
    }


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
            "direct_connector": "available for status and approved direct task envelopes only",
            "non_overlap_rule": (
                "Zeus should keep product intent, supervision, and escalation. "
                "Factory/olga-openhands owns development execution."
            ),
        },
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
    repo, QA, or software-delivery work. It creates the branch Kanban project
    and canonical stage tasks before delegating asynchronously to Leo.
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
    metadata = {
        "canonical_factory_project": True,
        "source": "zeus_factory_project_request",
        "project_id": pid,
        "project_slug": slug,
        "repo_name": repo_name,
        "repo_visibility": "private",
        "preview_url_expected": preview_url,
        "notion_required": True,
        "playwright_required": True,
        "footer_credit_required": "desarrollado por: SitioUno Factory",
        "complexity": complexity,
        "autonomy_level": autonomy_level,
        "stage_order": [stage[1] for stage in FACTORY_CANONICAL_STAGES],
    }

    project_task = {
        "id": pid,
        "branch": "sicilia",
        "title": title,
        "description": request[:2000],
        "status": "claimed",
        "priority": 2 if complexity in {"standard", "complex"} else 3,
        "agent_id": "leo-orquestador",
        "role": "factory-coordinator",
        "project_id": pid,
        "initiative_id": pid,
        "source": "zeus_factory_project_request",
        "metadata": metadata,
    }
    stage_tasks = [
        {
            "id": f"{pid}:{stage_key}",
            "branch": "sicilia",
            "title": f"{pid} {stage_name}",
            "description": f"Canonical Factory stage for {title}. Owner: {owner}.",
            "status": status,
            "priority": 2,
            "agent_id": owner,
            "role": "factory-stage-owner",
            "project_id": pid,
            "initiative_id": pid,
            "parent_id": pid,
            "source": "zeus_factory_project_request",
            "metadata": {**metadata, "stage": stage_name, "stage_key": stage_key},
        }
        for stage_key, stage_name, owner, status in FACTORY_CANONICAL_STAGES
    ]
    task_spec = "\n".join(
        [
            "TASKSPEC_CANONICAL_FACTORY_PROJECT",
            f"Project ID: {pid}",
            f"Project slug: {slug}",
            f"Title: {title}",
            f"Complexity: {complexity}",
            f"Autonomy level: {autonomy_level}",
            f"Required private repo: https://github.com/SiteOneTech/{repo_name}",
            f"Required preview target: {preview_url}",
            "Required footer credit on every public preview page: desarrollado por: SitioUno Factory",
            "",
            "Jean's request:",
            request,
            "",
            "Mandatory route:",
            "1. Do not satisfy this by creating a loose file under /home or by only answering in chat.",
            "2. Use the existing branch Kanban project and stage tasks created by Zeus for this project_id.",
            "3. Follow IDEA -> DISCOVERY -> PRODUCT_SHAPING -> ARCHITECTURE_REVIEW -> READY_FOR_SPRINT -> EXECUTION -> CODE_REVIEW -> QA_VALIDATION -> SECURITY_REVIEW -> ZEUS_ACCEPTANCE -> RELEASE -> RETROSPECTIVE -> MEMORY_UPDATE.",
            "4. Create or identify the private GitHub repo named factory-su-<project-slug>.",
            "5. Produce project docs/evidence: PRD, architecture brief, sprint plan, execution log, code review, QA report, Browser QA/Playwright report, security review, release handoff, retrospective, metrics.",
            "6. Publish user-facing work to the KIDU Preview Lab unless Zeus/Jean explicitly says not to publish.",
            "7. Record Notion human-readable evidence through Zeus or the approved writer.",
            "8. If any QA/security/release gate fails, mark REGATE_REQUIRED and do not present the work as accepted.",
            "9. Report progress asynchronously; Zeus will poll branch Kanban and delegation status.",
        ]
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "project_id": pid,
            "project_slug": slug,
            "repo_name": repo_name,
            "preview_url_expected": preview_url,
            "kanban_project_task": project_task,
            "kanban_stage_tasks": stage_tasks,
            "delegation_task_spec": task_spec,
        }

    kanban_results = [_branch_kanban_write("sicilia", "/v1/kanban/tasks", project_task)]
    for stage_task in stage_tasks:
        kanban_results.append(_branch_kanban_write("sicilia", "/v1/kanban/tasks", stage_task))
    kanban_results.append(
        _branch_kanban_write(
            "sicilia",
            "/v1/kanban/events",
            {
                "event_type": "factory_project_submitted",
                "task_id": pid,
                "branch": "sicilia",
                "agent_id": "leo-orquestador",
                "message": f"Zeus submitted canonical Factory project: {title}",
                "status": "claimed",
                "metadata": metadata,
            },
        )
    )
    failed_writes = [item for item in kanban_results if not item.get("ok")]
    if failed_writes:
        return {
            "ok": False,
            "error": "branch Kanban write failed; refusing to delegate untracked Factory work",
            "project_id": pid,
            "failed_writes": failed_writes[:5],
            "kanban_results": kanban_results,
        }

    notion_event = openclaw_notion_log_event(
        title=f"{pid} submitted to Factory",
        summary=f"{title}\n\nExpected repo: {repo_name}\nExpected preview: {preview_url}",
        category="factory",
        status="submitted",
        preview_url=preview_url,
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
    return {
        "ok": bool(delegation.get("ok")),
        "project_id": pid,
        "project_slug": slug,
        "repo_name": repo_name,
        "preview_url_expected": preview_url,
        "kanban_created": True,
        "kanban_results_count": len(kanban_results),
        "notion_event": notion_event,
        "delegation": delegation,
        "next_poll": {
            "tool": "openclaw_delegation_status",
            "office_id": "sicilia",
            "task_id": delegation.get("task_id"),
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
    agent, which owns OpenHands execution. Set execute=false to validate the
    route without starting work. Direct connector execution is available only
    by setting route="direct" and execute=true.
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

    if route != "direct":
        return {"ok": False, "error": "route must be 'factory' or 'direct'"}

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
            return {
                "ok": True,
                "http_status": resp.status,
                "duration_s": round(time.monotonic() - started, 3),
                "task_id": task_id,
                "async_mode": async_mode,
                "response": parsed,
            }
    except urllib.error.HTTPError as exc:
        text = exc.read(4096).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "duration_s": round(time.monotonic() - started, 3),
            "task_id": task_id,
            "async_mode": async_mode,
            "error": text or str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "duration_s": round(time.monotonic() - started, 3),
            "task_id": task_id,
            "async_mode": async_mode,
            "error": str(exc),
        }


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
            "For OpenHands work, prefer Zeus -> Sicilia -> olga-openhands -> OpenHands runner; use the direct OpenHands connector only for approved status checks or explicit direct task envelopes.",
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

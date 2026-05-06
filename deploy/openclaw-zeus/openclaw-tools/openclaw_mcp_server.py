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
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - Hermes ships PyYAML, fallback is for diagnostics.
    yaml = None

from mcp.server.fastmcp import FastMCP


DEFAULT_CONFIG = Path.home() / ".hermes" / "openclaw-tools" / "openclaw-fleet.yaml"
MAX_DEADLINE_S = 600
DEFAULT_TIMEOUT_S = 8

mcp = FastMCP("openclaw-office")


def _config_path() -> Path:
    return Path(os.getenv("OPENCLAW_FLEET_CONFIG", str(DEFAULT_CONFIG))).expanduser()


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


def _offices() -> dict[str, dict[str, Any]]:
    cfg = _load_config()
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


def _http_probe(url: str, timeout_s: float = 5.0) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "no url configured"}
    started = time.monotonic()
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read(240).decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status": resp.status,
                "body_preview": body,
                "duration_s": round(time.monotonic() - started, 3),
            }
    except urllib.error.HTTPError as exc:
        # HTTP 405/501 on GET still proves the endpoint is reachable.
        body = exc.read(240).decode("utf-8", errors="replace")
        return {
            "ok": True,
            "status": exc.code,
            "body_preview": body,
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }


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
        "token_configured": bool(os.getenv(str(office.get("token_env") or ""))),
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
            item["delegate_probe"] = _http_probe(endpoint)
    return item


@mcp.tool()
def openclaw_identity() -> dict[str, Any]:
    """Return Hermes' SitioUno/OpenClaw identity and the control-plane pattern."""
    cfg = _load_config()
    return {
        "ok": True,
        "site": cfg.get("site") or {},
        "pattern": {
            "identity_source": "~/.hermes/SOUL.md",
            "office_registry": str(_config_path()),
            "delegation_contract": "branch-delegation-v1",
            "transport": "HTTP POST /v1/delegate over Tailscale or approved private network",
            "security": "Bearer token per office via environment variable; no secrets in YAML",
        },
    }


@mcp.tool()
def openclaw_list_offices(include_live: bool = True) -> dict[str, Any]:
    """List known OpenClaw offices, agents, endpoints, and live Tailscale status."""
    cfg = _load_config()
    offices = _offices()
    return {
        "ok": True,
        "site": cfg.get("site") or {},
        "config_path": str(_config_path()),
        "offices": {
            office_id: _redacted_office(office_id, office, include_live=include_live)
            for office_id, office in sorted(offices.items())
        },
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
) -> dict[str, Any]:
    """Delegate a task to an OpenClaw office agent using branch-delegation-v1.

    Set dry_run=true to validate routing and show the request envelope without
    contacting the remote office.
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
    token = os.getenv(token_env) if token_env else ""
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

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
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
    try:
        with urllib.request.urlopen(req, timeout=deadline + DEFAULT_TIMEOUT_S) as resp:
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
                "response": parsed,
            }
    except urllib.error.HTTPError as exc:
        text = exc.read(4096).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "duration_s": round(time.monotonic() - started, 3),
            "task_id": task_id,
            "error": text or str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "duration_s": round(time.monotonic() - started, 3),
            "task_id": task_id,
            "error": str(exc),
        }


@mcp.tool()
def openclaw_delegation_runbook() -> dict[str, Any]:
    """Explain the recommended architecture for adding offices and bots to Hermes."""
    return {
        "ok": True,
        "steps": [
            "Register each office in openclaw-fleet.yaml with node id, display name, tailnet host/IP, agents, endpoint, and token env.",
            "Expose each office through the branch-delegation-v1 receiver or an approved gateway/queue adapter.",
            "Store each office token only in ~/.hermes/.env as OPENCLAW_<OFFICE>_DELEGATE_TOKEN.",
            "Enable the openclaw-office MCP server in Hermes for CLI and Telegram toolsets.",
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

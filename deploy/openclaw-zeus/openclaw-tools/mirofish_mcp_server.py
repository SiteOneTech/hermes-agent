#!/usr/bin/env python3
"""MCP tools for using MiroFish as a Zeus service.

The wrapper is intentionally conservative:
- Default transport is Zeus -> MiroFish over the private GCP VPC.
- Read-only endpoints are enabled by default.
- Endpoints that can spend LLM tokens or mutate simulation state require
  MIROFISH_ENABLE_EXPENSIVE_TOOLS=1.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP


DEFAULT_BASE_URL = "http://10.42.0.4"
DEFAULT_TIMEOUT_S = 20
MAX_RESPONSE_CHARS = 12000

mcp = FastMCP("mirofish")


def _base_url() -> str:
    return os.getenv("MIROFISH_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _expensive_enabled() -> bool:
    return os.getenv("MIROFISH_ENABLE_EXPENSIVE_TOOLS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _url(path: str, params: dict[str, Any] | None = None) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"{_base_url()}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
    return url


def _truncate(value: Any, max_chars: int = MAX_RESPONSE_CHARS) -> tuple[Any, bool]:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return value, False
    return text[:max_chars] + "\n...<truncated>", True


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> dict[str, Any]:
    started = time.monotonic()
    body = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(_url(path, params), data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:
        return {
            "ok": False,
            "method": method,
            "path": path,
            "base_url": _base_url(),
            "error": str(exc),
            "duration_s": round(time.monotonic() - started, 3),
        }

    try:
        parsed: Any = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw

    response, truncated = _truncate(parsed, max_chars=max_chars)
    return {
        "ok": 200 <= status < 300,
        "method": method,
        "path": path,
        "status": status,
        "base_url": _base_url(),
        "data": response,
        "truncated": truncated,
        "duration_s": round(time.monotonic() - started, 3),
    }


def _blocked_expensive_tool(name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": name,
        "error": "This MiroFish tool may spend LLM tokens or mutate state. Set MIROFISH_ENABLE_EXPENSIVE_TOOLS=1 in the MCP server env to enable it.",
    }


@mcp.tool()
def mirofish_service_info() -> dict[str, Any]:
    """Return MiroFish service identity, base URL, and available Zeus tool policy."""
    return {
        "ok": True,
        "service": "MiroFish",
        "base_url": _base_url(),
        "transport": "Zeus -> MiroFish over private GCP VPC or Tailscale",
        "read_tools_enabled": True,
        "expensive_tools_enabled": _expensive_enabled(),
        "core_api_groups": {
            "graph": "/api/graph",
            "simulation": "/api/simulation",
            "report": "/api/report",
        },
        "safe_tools": [
            "mirofish_health",
            "mirofish_list_projects",
            "mirofish_get_project",
            "mirofish_list_tasks",
            "mirofish_get_task",
            "mirofish_list_simulations",
            "mirofish_get_simulation",
            "mirofish_simulation_history",
            "mirofish_run_status",
            "mirofish_list_reports",
            "mirofish_get_report",
            "mirofish_report_sections",
            "mirofish_get_api_path",
        ],
        "gated_tools": [
            "mirofish_report_chat",
        ],
    }


@mcp.tool()
def mirofish_health() -> dict[str, Any]:
    """Check that the MiroFish backend is reachable."""
    return _request("GET", "/health")


@mcp.tool()
def mirofish_get_api_path(path: str, max_chars: int = MAX_RESPONSE_CHARS) -> dict[str, Any]:
    """GET any MiroFish API path for read-only inspection."""
    path = str(path or "").strip()
    if path != "/health" and not path.startswith("/api/"):
        return {"ok": False, "error": "Only /health or /api/... paths are allowed"}
    return _request("GET", path, max_chars=max(1000, min(int(max_chars), 50000)))


@mcp.tool()
def mirofish_list_projects(limit: int = 50) -> dict[str, Any]:
    """List MiroFish projects."""
    return _request("GET", "/api/graph/project/list", params={"limit": int(limit)})


@mcp.tool()
def mirofish_get_project(project_id: str) -> dict[str, Any]:
    """Get one MiroFish project by project_id."""
    return _request("GET", f"/api/graph/project/{urllib.parse.quote(project_id, safe='')}")


@mcp.tool()
def mirofish_list_tasks(status: str | None = None) -> dict[str, Any]:
    """List graph/task jobs known to MiroFish."""
    return _request("GET", "/api/graph/tasks", params={"status": status})


@mcp.tool()
def mirofish_get_task(task_id: str) -> dict[str, Any]:
    """Get one MiroFish async task by task_id."""
    return _request("GET", f"/api/graph/task/{urllib.parse.quote(task_id, safe='')}")


@mcp.tool()
def mirofish_list_simulations(project_id: str | None = None) -> dict[str, Any]:
    """List MiroFish simulations, optionally filtered by project_id."""
    return _request("GET", "/api/simulation/list", params={"project_id": project_id})


@mcp.tool()
def mirofish_get_simulation(simulation_id: str) -> dict[str, Any]:
    """Get one MiroFish simulation by simulation_id."""
    return _request("GET", f"/api/simulation/{urllib.parse.quote(simulation_id, safe='')}")


@mcp.tool()
def mirofish_simulation_history(limit: int = 20) -> dict[str, Any]:
    """Get the MiroFish simulation history shown on the home screen."""
    return _request("GET", "/api/simulation/history", params={"limit": int(limit)})


@mcp.tool()
def mirofish_run_status(simulation_id: str, detail: bool = False) -> dict[str, Any]:
    """Get current run status for a simulation."""
    suffix = "run-status/detail" if detail else "run-status"
    return _request("GET", f"/api/simulation/{urllib.parse.quote(simulation_id, safe='')}/{suffix}")


@mcp.tool()
def mirofish_list_reports() -> dict[str, Any]:
    """List generated MiroFish reports."""
    return _request("GET", "/api/report/list")


@mcp.tool()
def mirofish_get_report(report_id: str, max_chars: int = MAX_RESPONSE_CHARS) -> dict[str, Any]:
    """Get one MiroFish report by report_id."""
    return _request(
        "GET",
        f"/api/report/{urllib.parse.quote(report_id, safe='')}",
        max_chars=max(1000, min(int(max_chars), 50000)),
    )


@mcp.tool()
def mirofish_report_sections(report_id: str) -> dict[str, Any]:
    """List sections in a generated report."""
    return _request("GET", f"/api/report/{urllib.parse.quote(report_id, safe='')}/sections")


@mcp.tool()
def mirofish_report_chat(
    simulation_id: str,
    message: str,
    chat_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Ask MiroFish ReportAgent a question about an existing simulation."""
    if not _expensive_enabled():
        return _blocked_expensive_tool("mirofish_report_chat")
    return _request(
        "POST",
        "/api/report/chat",
        json_body={
            "simulation_id": simulation_id,
            "message": message,
            "chat_history": chat_history or [],
        },
        timeout_s=120,
        max_chars=50000,
    )


def _self_test() -> None:
    print(json.dumps(mirofish_health(), ensure_ascii=False, indent=2))
    print(json.dumps(mirofish_list_projects(limit=5), ensure_ascii=False, indent=2))
    print(json.dumps(mirofish_simulation_history(limit=5), ensure_ascii=False, indent=2))


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

#!/usr/bin/env python3
"""Export Honcho-hosted Zeus memory to local JSON/JSONL snapshots.

The backup intentionally stores operational data only. It never writes the
Honcho API key into backup files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from honcho import Honcho


DEFAULT_WORKSPACE = "sitio-uno-gcp"
DEFAULT_OUTPUT_DIR = Path.home() / ".hermes" / "honcho-backups"
HERMES_HOME = Path.home() / ".hermes"


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if hasattr(value, "dict"):
        try:
            return _jsonable(value.dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _jsonable(
            {
                k: v
                for k, v in vars(value).items()
                if not k.startswith("_") and k not in {"api_key"}
            }
        )
    return str(value)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for value in values:
            f.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True) + "\n")


def _try(label: str, fn) -> dict[str, Any]:
    try:
        return {"ok": True, "data": fn()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "label": label}


def _all_items(page_factory, *, size: int = 100) -> list[Any]:
    return list(page_factory(size=size))


def _configured_workspaces(config: dict[str, Any]) -> list[str]:
    found: list[str] = []
    root_workspace = config.get("workspace")
    if isinstance(root_workspace, str) and root_workspace.strip():
        found.append(root_workspace.strip())
    hosts = config.get("hosts") or {}
    if isinstance(hosts, dict):
        for block in hosts.values():
            if isinstance(block, dict):
                workspace = block.get("workspace")
                if isinstance(workspace, str) and workspace.strip():
                    found.append(workspace.strip())
    if not found:
        found.append(DEFAULT_WORKSPACE)
    return sorted(set(found))


def _client(api_key: str, workspace_id: str, config: dict[str, Any]) -> Honcho:
    base_url = (
        config.get("baseUrl")
        or config.get("base_url")
        or os.environ.get("HONCHO_BASE_URL")
        or None
    )
    kwargs: dict[str, Any] = {"api_key": api_key, "workspace_id": workspace_id}
    if base_url:
        kwargs["base_url"] = base_url
    return Honcho(**kwargs)


def export_workspace(api_key: str, workspace_id: str, config: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    client = _client(api_key, workspace_id, config)
    workspace_dir = out_dir / "workspaces" / workspace_id
    workspace_dir.mkdir(parents=True, exist_ok=True)

    peers = _all_items(client.peers)
    sessions = _all_items(client.sessions)

    _write_jsonl(workspace_dir / "peers.jsonl", peers)
    _write_jsonl(workspace_dir / "sessions.jsonl", sessions)
    _write_json(workspace_dir / "metadata.json", _try("workspace_metadata", client.get_metadata))
    _write_json(workspace_dir / "configuration.json", _try("workspace_configuration", client.get_configuration))
    _write_json(workspace_dir / "queue_status.json", _try("workspace_queue_status", client.queue_status))

    session_summaries: dict[str, Any] = {}
    session_peers: dict[str, Any] = {}
    message_count = 0
    for session in sessions:
        sid = getattr(session, "id", None)
        if not sid:
            continue
        messages = _all_items(session.messages)
        message_count += len(messages)
        _write_jsonl(workspace_dir / "sessions" / sid / "messages.jsonl", messages)
        _write_json(workspace_dir / "sessions" / sid / "metadata.json", _try(f"{sid}.metadata", session.get_metadata))
        _write_json(workspace_dir / "sessions" / sid / "configuration.json", _try(f"{sid}.configuration", session.get_configuration))
        session_summaries[sid] = _try(f"{sid}.summaries", session.summaries)
        session_peers[sid] = _try(f"{sid}.peers", session.peers)
    _write_json(workspace_dir / "session_summaries.json", session_summaries)
    _write_json(workspace_dir / "session_peers.json", session_peers)

    peer_cards: dict[str, Any] = {}
    peer_metadata: dict[str, Any] = {}
    peer_config: dict[str, Any] = {}
    peer_representations: dict[str, Any] = {}
    conclusions: list[Any] = []
    peer_ids = [getattr(peer, "id", "") for peer in peers if getattr(peer, "id", "")]
    for peer in peers:
        pid = getattr(peer, "id", None)
        if not pid:
            continue
        peer_cards[pid] = _try(f"{pid}.card", peer.get_card)
        peer_metadata[pid] = _try(f"{pid}.metadata", peer.get_metadata)
        peer_config[pid] = _try(f"{pid}.configuration", peer.get_configuration)
        peer_representations[pid] = _try(f"{pid}.representation", peer.representation)
        for conclusion in peer.conclusions.list(size=100):
            conclusions.append(conclusion)

    # For small workspaces, also preserve directional conclusions between peers.
    if len(peer_ids) <= 50:
        seen = {getattr(c, "id", None) for c in conclusions}
        for observer in peers:
            observer_id = getattr(observer, "id", None)
            if not observer_id:
                continue
            for observed_id in peer_ids:
                if observed_id == observer_id:
                    continue
                try:
                    for conclusion in observer.conclusions_of(observed_id).list(size=100):
                        cid = getattr(conclusion, "id", None)
                        if cid not in seen:
                            conclusions.append(conclusion)
                            seen.add(cid)
                except Exception:
                    continue

    _write_json(workspace_dir / "peer_cards.json", peer_cards)
    _write_json(workspace_dir / "peer_metadata.json", peer_metadata)
    _write_json(workspace_dir / "peer_configuration.json", peer_config)
    _write_json(workspace_dir / "peer_representations.json", peer_representations)
    _write_jsonl(workspace_dir / "conclusions.jsonl", conclusions)

    return {
        "workspace_id": workspace_id,
        "peers": len(peers),
        "sessions": len(sessions),
        "messages": message_count,
        "conclusions": len(conclusions),
    }


def prune_old_backups(root: Path, keep: int) -> None:
    if keep <= 0 or not root.exists():
        return
    runs = sorted([p for p in root.iterdir() if p.is_dir() and p.name.startswith("run-")])
    for path in runs[:-keep]:
        shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--workspace", action="append", help="Workspace to export; repeatable")
    parser.add_argument("--keep", type=int, default=30, help="Number of backup runs to retain")
    args = parser.parse_args()

    env = _load_dotenv(HERMES_HOME / ".env")
    api_key = env.get("HONCHO_API_KEY") or os.environ.get("HONCHO_API_KEY")
    if not api_key:
        print("HONCHO_API_KEY is not configured", file=sys.stderr)
        return 2

    config = _load_json(HERMES_HOME / "honcho.json")
    workspaces = args.workspace or _configured_workspaces(config)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = Path(args.output_dir).expanduser()
    run_dir = out_root / f"run-{now}"
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "exported_at": now,
        "workspace_ids": workspaces,
        "source": "honcho-hosted",
        "contains_api_key": False,
        "results": [],
    }
    for workspace_id in workspaces:
        try:
            manifest["results"].append(export_workspace(api_key, workspace_id, config, run_dir))
        except Exception as exc:
            manifest["results"].append(
                {
                    "workspace_id": workspace_id,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    _write_json(run_dir / "manifest.json", manifest)
    latest = out_root / "latest"
    tmp_latest = out_root / ".latest.tmp"
    if tmp_latest.exists() or tmp_latest.is_symlink():
        tmp_latest.unlink()
    tmp_latest.symlink_to(run_dir.name)
    tmp_latest.replace(latest)
    prune_old_backups(out_root, args.keep)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

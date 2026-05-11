import importlib.util
from pathlib import Path


def load_openclaw_tools():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "openclaw-zeus"
        / "openclaw-tools"
        / "openclaw_mcp_server.py"
    )
    spec = importlib.util.spec_from_file_location("openclaw_mcp_server_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_orchestration_refs_do_not_parse_engine_list_as_work_order():
    tools = load_openclaw_tools()
    task = "\n".join(
        [
            "Workflow run ID: wf_run_123",
            "Execution engines must be explicit in every code-bearing work order: codex, claude_code, openhands_vm.",
        ]
    )

    workflow_run_id, work_order_id = tools._orchestration_refs_from_task(
        task,
        {"metadata": {"workflow_run_id": "wf_run_123"}},
    )

    assert workflow_run_id == "wf_run_123"
    assert work_order_id == ""


def test_orchestration_refs_use_initial_work_order_metadata():
    tools = load_openclaw_tools()

    workflow_run_id, work_order_id = tools._orchestration_refs_from_task(
        "Execution engines must be explicit in every code-bearing work order: codex.",
        {
            "metadata": {
                "workflow_run_id": "wf_run_123",
                "delegation_context": {"initial_work_order_id": "wo_abc123"},
            }
        },
    )

    assert workflow_run_id == "wf_run_123"
    assert work_order_id == "wo_abc123"


def test_factory_initial_delegation_work_order_prefers_leo_idea():
    tools = load_openclaw_tools()
    work_orders = [
        {
            "work_order_id": "wo_discovery",
            "owner_role": "vera-research",
            "inputs": {"stage": "DISCOVERY"},
        },
        {
            "work_order_id": "wo_idea",
            "owner_role": "leo-orquestador",
            "inputs": {"stage": "IDEA"},
        },
        {
            "work_order_id": "wo_ready",
            "owner_role": "leo-orquestador",
            "inputs": {"stage": "READY_FOR_SPRINT"},
        },
    ]

    assert tools._factory_initial_delegation_work_order_id(work_orders) == "wo_idea"


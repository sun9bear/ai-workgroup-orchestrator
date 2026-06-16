from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import yaml

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_PATH = PROJECT_ROOT / "docs" / "examples" / "hermes-mcp-aiwg-readonly.yaml"
GUIDE_PATH = PROJECT_ROOT / "docs" / "guides" / "phase-c-hermes-mcp-readonly-client.md"
EXPECTED_TOOLS = {"status", "list_tasks", "get_task", "recent_events"}
FORBIDDEN_TOOL_WORDS = {
    "claim",
    "update_status",
    "write_message",
    "approve",
    "start_real_agent",
    "dispatch_real_agent",
}


def write_config(tmp_path: Path) -> Path:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config), encoding="utf-8")
    return path


def write_message(project_root: Path) -> None:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / "2026-06-06T140000_from-CodeX_to-Fake_type-instruction_task-C3-mcp-config-example-smoke.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                "id: C3-msg-config-example",
                "task: C3-mcp-config-example-smoke",
                "from: CodeX",
                "to: Fake",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T14:00:00+08:00",
                "can_write: false",
                "context_files:",
                "  - docs/ai-workgroup/00-protocol.md",
                "allowed_files: []",
                "forbidden_files:",
                "  - .env",
                "acceptance: []",
                'claimed_by: ""',
                'claimed_at: ""',
                'lock_id: ""',
                "attempt: 0",
                "max_attempts: 2",
                "timeout_minutes: 30",
                "review_delegate: CodeX",
                "---",
                "",
                "# Phase C3 MCP config example smoke fixture",
                "",
            ]
        ),
        encoding="utf-8",
    )


def seed_fake_done_task(tmp_path: Path) -> Path:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config_path = write_config(tmp_path)
    init_database(config=config, project_root=tmp_path)
    write_message(tmp_path)
    result = run_once(config=config, project_root=tmp_path, agent="Fake")
    assert result.status == "done"
    return config_path


def decode_tool_result(result) -> dict:
    assert result.content, "MCP call_tool returned no content"
    first = result.content[0]
    if hasattr(first, "text"):
        return json.loads(first.text)
    if isinstance(first, dict) and "text" in first:
        return json.loads(first["text"])
    raise AssertionError(f"Unsupported MCP result content: {first!r}")


def load_example() -> dict:
    return yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))


def test_hermes_mcp_example_is_parseable_read_only_stdio_config() -> None:
    data = load_example()
    assert set(data) == {"mcp_servers"}
    servers = data["mcp_servers"]
    assert set(servers) == {"aiwg_readonly"}
    server = servers["aiwg_readonly"]

    assert server["command"] == "python"
    assert server["args"] == [
        "-m",
        "aiwg.mcp.server",
        "--config",
        "D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml",
    ]
    assert server["cwd"] == "D:/AIGroup/ai-workgroup-orchestrator"
    assert server["timeout"] >= 60
    assert server["connect_timeout"] >= 30
    assert server.get("sampling", {}).get("enabled") is False
    assert "env" not in server or server["env"] == {}
    assert "url" not in server


def test_example_does_not_expose_write_or_real_agent_tools() -> None:
    example_text = EXAMPLE_PATH.read_text(encoding="utf-8").lower()
    assert "mcp_aiwg_readonly_status" in example_text
    assert "mcp_aiwg_readonly_list_tasks" in example_text
    assert "mcp_aiwg_readonly_get_task" in example_text
    assert "mcp_aiwg_readonly_recent_events" in example_text
    assert "no secrets" in example_text
    assert "read-only" in example_text
    for forbidden in FORBIDDEN_TOOL_WORDS:
        assert forbidden not in example_text


def test_guide_documents_copy_steps_and_safety_boundaries() -> None:
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert "docs/examples/hermes-mcp-aiwg-readonly.yaml" in guide
    assert "~/.hermes/config.yaml" in guide
    assert "hermes mcp list" in guide
    assert "hermes mcp test aiwg_readonly" in guide
    assert "python -m aiwg.mcp.server --list-tools" in guide
    assert "python -m aiwg.cli doctor" in guide
    assert "AIVideoTrans" in guide
    assert "不修改" in guide
    assert "不启用真实 agent" in guide
    for tool in sorted(EXPECTED_TOOLS):
        assert tool in guide


def test_example_config_can_drive_stdio_mcp_smoke(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    example = load_example()
    server = example["mcp_servers"]["aiwg_readonly"]
    config_path = seed_fake_done_task(tmp_path)
    args = [str(config_path) if arg == "D:/AIGroup/ai-workgroup-orchestrator/aiwg.yaml" else arg for arg in server["args"]]

    async def run_smoke() -> None:
        params = StdioServerParameters(
            command=sys.executable if server["command"] == "python" else server["command"],
            args=args,
            cwd=str(PROJECT_ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
                listed = decode_tool_result(await session.call_tool("list_tasks", {"limit": 5}))
                assert listed["capabilities"]["read_only"] is True
                assert listed["capabilities"]["mutation_actions"] == []
                assert listed["count"] == 1

    asyncio.run(run_smoke())

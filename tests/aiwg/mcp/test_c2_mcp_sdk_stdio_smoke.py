from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def write_config(tmp_path: Path) -> Path:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config), encoding="utf-8")
    return path


def write_message(project_root: Path, *, message_id: str, task: str) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-06T130000_from-CodeX_to-Fake_type-instruction_task-{task}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {task}",
                "from: CodeX",
                "to: Fake",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T13:00:00+08:00",
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
                "# Phase C2 MCP SDK smoke fixture",
                "",
                "用于 Phase C2 stdio MCP client smoke 测试。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def seed_fake_done_task(tmp_path: Path) -> Path:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config_path = write_config(tmp_path)
    init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="C2-msg-stdio", task="C2-mcp-stdio-smoke")
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
    raise AssertionError(f"Unsupported MCP tool result content: {first!r}")


def test_pyproject_declares_mcp_optional_dependency() -> None:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional = data["project"]["optional-dependencies"]

    assert "mcp" in optional
    mcp_requirements = optional["mcp"]
    assert any(requirement.startswith("mcp>=") for requirement in mcp_requirements)
    assert any("starlette<0.47" in requirement for requirement in mcp_requirements)
    assert any("sse-starlette" in requirement and "<3" in requirement for requirement in mcp_requirements)


def test_mcp_sdk_importable_and_server_sdk_check_succeeds(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"

    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.mcp.server", "--config", str(config_path), "--require-sdk-check-only"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert "MCP SDK available" in completed.stdout
    assert db_path.exists() is False


def test_stdio_mcp_client_can_list_and_call_read_only_tools(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config_path = seed_fake_done_task(tmp_path)

    async def run_smoke() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aiwg.mcp.server", "--config", str(config_path)],
            cwd=str(PROJECT_ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = {tool.name for tool in tools.tools}
                assert tool_names == {"status", "list_tasks", "get_task", "recent_events"}

                listed = decode_tool_result(await session.call_tool("list_tasks", {"limit": 5}))
                assert listed["tool"] == "list_tasks"
                assert listed["capabilities"]["read_only"] is True
                assert listed["capabilities"]["mutation_actions"] == []
                assert listed["count"] == 1
                task_id = listed["tasks"][0]["id"]

                task = decode_tool_result(await session.call_tool("get_task", {"task_id": task_id}))
                assert task["found"] is True
                assert task["task"]["id"] == task_id

                status = decode_tool_result(await session.call_tool("status", {"recent_events": 2, "task_limit": 5}))
                assert status["capabilities"]["read_only"] is True
                assert status["summary"]["total_tasks"] == 1

    asyncio.run(run_smoke())

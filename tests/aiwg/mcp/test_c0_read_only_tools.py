from __future__ import annotations

import sqlite3
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def write_config(tmp_path: Path, config: dict | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def write_message(project_root: Path, *, message_id: str, task: str) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-06T120000_from-CodeX_to-Fake_type-instruction_task-{task}.md"
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
                "created_at: 2026-06-06T12:00:00+08:00",
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
                "# Phase C MCP read-only fixture",
                "",
                "用于 Phase C 只读 MCP tools 测试。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def db_digest(db_path: Path) -> tuple:
    with sqlite3.connect(db_path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0],
            conn.execute("SELECT id, status, attempt, completed_at FROM tasks ORDER BY id").fetchall(),
        )


def seed_fake_done_task(tmp_path: Path, *, message_id: str = "C0-msg-001") -> tuple[dict, Path, Path]:
    config = build_test_config(tmp_path)
    config_path = write_config(tmp_path, config)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id=message_id, task="C0-mcp-read-only-tools")
    result = run_once(config=config, project_root=tmp_path, agent="Fake")
    assert result.status == "done"
    return config, config_path, db_path


def test_status_tool_returns_read_only_snapshot_without_mutating_db(tmp_path: Path) -> None:
    from aiwg.mcp.tools import status_tool

    _config, config_path, db_path = seed_fake_done_task(tmp_path, message_id="C0-msg-status")
    before = db_digest(db_path)

    payload = status_tool(config_path=str(config_path), recent_events=4, task_limit=10)

    assert db_digest(db_path) == before
    assert payload["tool"] == "status"
    assert payload["capabilities"]["read_only"] is True
    assert payload["capabilities"]["mutation_actions"] == []
    assert payload["database"]["exists"] is True
    assert payload["summary"]["total_tasks"] == 1
    assert payload["tasks"][0]["id"] == "C0-msg-status"
    assert payload["tasks"][0]["status"] == "done"
    assert len(payload["recent_events"]) == 4


def test_list_tasks_tool_filters_tasks_without_mutating_db(tmp_path: Path) -> None:
    from aiwg.mcp.tools import list_tasks_tool

    _config, config_path, db_path = seed_fake_done_task(tmp_path, message_id="C0-msg-list")
    before = db_digest(db_path)

    payload = list_tasks_tool(config_path=str(config_path), status_filter="done", agent="Fake", limit=5)

    assert db_digest(db_path) == before
    assert payload["tool"] == "list_tasks"
    assert payload["capabilities"]["read_only"] is True
    assert payload["count"] == 1
    assert payload["tasks"] == [
        {
            "id": "C0-msg-list",
            "task_id": "C0-mcp-read-only-tools",
            "status": "done",
            "to_agent": "Fake",
            "from_agent": "CodeX",
            "requires_human": False,
            "can_write": False,
            "latest_report_path": payload["tasks"][0]["latest_report_path"],
        }
    ]
    assert payload["tasks"][0]["latest_report_path"].endswith("report.md")


def test_get_task_tool_returns_found_false_for_missing_task_without_mutation(tmp_path: Path) -> None:
    from aiwg.mcp.tools import get_task_tool

    _config, config_path, db_path = seed_fake_done_task(tmp_path, message_id="C0-msg-get")
    before = db_digest(db_path)

    found = get_task_tool(config_path=str(config_path), task_id="C0-msg-get")
    missing = get_task_tool(config_path=str(config_path), task_id="does-not-exist")

    assert db_digest(db_path) == before
    assert found["tool"] == "get_task"
    assert found["found"] is True
    assert found["task"]["id"] == "C0-msg-get"
    assert found["task"]["status"] == "done"
    assert found["task"]["latest_report_path"].endswith("report.md")
    assert missing == {
        "tool": "get_task",
        "capabilities": {"read_only": True, "mutation_actions": []},
        "found": False,
        "task": None,
        "task_id": "does-not-exist",
    }


def test_recent_events_tool_returns_newest_events_without_mutation(tmp_path: Path) -> None:
    from aiwg.mcp.tools import recent_events_tool

    _config, config_path, db_path = seed_fake_done_task(tmp_path, message_id="C0-msg-events")
    before = db_digest(db_path)

    payload = recent_events_tool(config_path=str(config_path), limit=3)

    assert db_digest(db_path) == before
    assert payload["tool"] == "recent_events"
    assert payload["capabilities"]["read_only"] is True
    assert payload["count"] == 3
    assert [event["type"] for event in payload["events"]] == [
        "task_done",
        "agent_run_succeeded",
        "fake_report_written",
    ]


def test_read_only_tools_do_not_create_missing_database(tmp_path: Path) -> None:
    from aiwg.mcp.tools import get_task_tool, list_tasks_tool, recent_events_tool, status_tool

    config = build_test_config(tmp_path)
    config_path = write_config(tmp_path, config)
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"

    status_payload = status_tool(config_path=str(config_path))
    list_payload = list_tasks_tool(config_path=str(config_path))
    get_payload = get_task_tool(config_path=str(config_path), task_id="missing")
    events_payload = recent_events_tool(config_path=str(config_path))

    assert db_path.exists() is False
    assert status_payload["database"]["exists"] is False
    assert list_payload["tasks"] == []
    assert list_payload["count"] == 0
    assert get_payload["found"] is False
    assert events_payload["events"] == []
    assert events_payload["count"] == 0

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database
from aiwg.state.importer import list_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def write_config(tmp_path: Path, config: dict | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def write_message(
    project_root: Path,
    *,
    agent: str = "Fake",
    to_agent: str = "Fake",
    message_id: str = "B1-msg-001",
    task: str = "B1-runtime-policy-gate",
    requires_human: bool = False,
    can_write: bool = False,
    allowed_files: list[str] | None = None,
) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / agent
        / f"2026-06-05T090000_from-CodeX_to-{to_agent}_type-instruction_task-{task}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    allowed_files = allowed_files or ([] if not can_write else ["docs/**"])
    lines = [
        "---",
        f"id: {message_id}",
        f"task: {task}",
        "from: CodeX",
        f"to: {to_agent}",
        "type: instruction",
        "status: ready",
        "priority: medium",
        'reply_to: ""',
        f"requires_human: {str(requires_human).lower()}",
        "created_at: 2026-06-05T09:00:00+08:00",
        f"can_write: {str(can_write).lower()}",
        "context_files:",
        "  - docs/ai-workgroup/00-protocol.md",
        "allowed_files:",
    ]
    if allowed_files:
        lines.extend(f"  - {item}" for item in allowed_files)
    else:
        lines[-1] = "allowed_files: []"
    lines.extend(
        [
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
            "# B1 fixture",
            "",
            "用于 Phase B1 runtime policy gate 测试。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def event_rows(db_path: Path) -> list[tuple[str, str | None, str | None, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT type, message_id, status, payload_json FROM events ORDER BY id"
        ).fetchall()


def test_pause_automation_kill_switch_denies_before_claim_or_dispatch(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    kill_switch = tmp_path / config["policy"]["global_kill_switch"]
    kill_switch.parent.mkdir(parents=True, exist_ok=True)
    kill_switch.write_text("paused by test\n", encoding="utf-8")
    write_message(tmp_path, message_id="B1-msg-paused")

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "policy_denied"
    assert result.message_id is None
    assert "PAUSE_AUTOMATION" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B1-msg-paused", "ready", 0)
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    rows = event_rows(db_path)
    assert [row[0] for row in rows] == ["task_imported", "policy_denied"]
    assert rows[-1][1] is None
    assert "pause_automation" in rows[-1][3]


def test_requires_human_task_is_moved_to_waiting_human_without_dispatch(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="B1-msg-human", requires_human=True)

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "policy_denied"
    assert result.message_id == "B1-msg-human"
    assert "requires_human" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B1-msg-human", "waiting_human", 0)
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    rows = event_rows(db_path)
    assert [row[0] for row in rows] == ["task_imported", "policy_denied"]
    assert rows[-1][1] == "B1-msg-human"
    assert rows[-1][2] == "waiting_human"


def test_write_task_is_denied_when_allow_write_false(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        message_id="B1-msg-write-denied",
        can_write=True,
        allowed_files=["docs/**"],
    )

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "policy_denied"
    assert result.message_id == "B1-msg-write-denied"
    assert "allow_write=false" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B1-msg-write-denied", "waiting_human", 0)
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    assert not (tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts").exists()


def test_real_agent_is_denied_when_allow_real_agents_false(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["agents"]["OpenCode"]["enabled"] = True
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        agent="OpenCode",
        to_agent="OpenCode",
        message_id="B1-msg-real-agent",
    )

    result = run_once(config=config, project_root=tmp_path, agent="OpenCode")

    assert result.status == "policy_denied"
    assert result.message_id is None
    assert "allow_real_agents=false" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="OpenCode")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B1-msg-real-agent", "ready", 0)
    ]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    rows = event_rows(db_path)
    assert [row[0] for row in rows] == ["task_imported", "policy_denied"]
    assert "safe_mode" in rows[-1][3]


def test_cli_run_once_policy_denied_is_a_safe_zero_exit(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["global_pause"] = True
    config_path = write_config(tmp_path, config)
    write_message(tmp_path, message_id="B1-msg-cli-paused")

    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "Fake"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert "status=policy_denied" in completed.stdout
    assert "message_id=-" in completed.stdout
    assert "global_pause" in completed.stdout

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.importer import list_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def shell_python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def yaml_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_message(
    project_root: Path,
    *,
    message_id: str,
    acceptance: list[str] | None = None,
) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-05T120000_from-CodeX_to-Fake_type-instruction_task-{message_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {message_id}",
        f"task: {message_id}",
        "from: CodeX",
        "to: Fake",
        "type: instruction",
        "status: ready",
        "priority: medium",
        'reply_to: ""',
        "requires_human: false",
        "created_at: 2026-06-05T12:00:00+08:00",
        "can_write: false",
        "context_files: []",
        "allowed_files: []",
        "forbidden_files:",
        "  - .env",
    ]
    if acceptance:
        lines.append("acceptance:")
        lines.extend(f"  - {yaml_single_quoted(command)}" for command in acceptance)
    else:
        lines.append("acceptance: []")
    lines.extend(
        [
            'claimed_by: ""',
            'claimed_at: ""',
            'lock_id: ""',
            "attempt: 0",
            "max_attempts: 2",
            "timeout_minutes: 30",
            "review_delegate: CodeX",
            "---",
            "",
            "# B3 verification fixture",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def event_types(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute("SELECT type FROM events ORDER BY id").fetchall()]


def verification_rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM verification_runs ORDER BY started_at, id").fetchall()]


def test_task_without_acceptance_commands_keeps_phase_a_done_without_verification_runs(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    write_message(tmp_path, message_id="B3-msg-no-acceptance", acceptance=[])

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "done"
    assert result.message_id == "B3-msg-no-acceptance"
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B3-msg-no-acceptance", "done", 1)
    ]
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    assert verification_rows(db_path) == []
    assert event_types(db_path) == [
        "task_imported",
        "task_claimed",
        "agent_run_started",
        "fake_report_written",
        "agent_run_succeeded",
        "task_done",
    ]


def test_successful_acceptance_command_records_verification_run_and_marks_done(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    command = shell_python_command("import sys; print(123); sys.exit(0)")
    write_message(tmp_path, message_id="B3-msg-verify-pass", acceptance=[command])

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "done"
    assert result.message_id == "B3-msg-verify-pass"
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    rows = verification_rows(db_path)
    assert len(rows) == 1
    run = rows[0]
    assert run["message_id"] == "B3-msg-verify-pass"
    assert run["command"] == command
    assert run["cwd"] == str(tmp_path)
    assert run["status"] == "succeeded"
    assert run["exit_code"] == 0
    assert Path(run["stdout_path"]).read_text(encoding="utf-8").strip() == "123"
    assert Path(run["stderr_path"]).read_text(encoding="utf-8") == ""
    assert event_types(db_path) == [
        "task_imported",
        "task_claimed",
        "agent_run_started",
        "fake_report_written",
        "agent_run_succeeded",
        "verification_run_started",
        "verification_run_succeeded",
        "task_done",
    ]


def test_failing_acceptance_command_records_failed_run_and_moves_task_to_needs_revision(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    command = shell_python_command("import sys; print(456); sys.exit(7)")
    write_message(tmp_path, message_id="B3-msg-verify-fail", acceptance=[command])

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "needs_revision"
    assert result.message_id == "B3-msg-verify-fail"
    assert "exit_code=7" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B3-msg-verify-fail", "needs_revision", 1)
    ]
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    rows = verification_rows(db_path)
    assert len(rows) == 1
    run = rows[0]
    assert run["status"] == "failed"
    assert run["exit_code"] == 7
    assert Path(run["stdout_path"]).read_text(encoding="utf-8").strip() == "456"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT completed_at FROM tasks WHERE id='B3-msg-verify-fail'").fetchone()[0] is None
        assert conn.execute("SELECT status, exit_code FROM agent_runs").fetchall() == [("succeeded", 0)]
    assert event_types(db_path) == [
        "task_imported",
        "task_claimed",
        "agent_run_started",
        "fake_report_written",
        "agent_run_succeeded",
        "verification_run_started",
        "verification_run_failed",
        "task_needs_revision",
    ]


def test_cli_status_json_exposes_verification_runs(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    command = shell_python_command("import sys; print(789); sys.exit(0)")
    write_message(tmp_path, message_id="B3-msg-cli-status", acceptance=[command])

    run_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "Fake"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert run_completed.returncode == 0, run_completed.stderr
    assert "status=done" in run_completed.stdout
    assert "message_id=B3-msg-cli-status" in run_completed.stdout

    status_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert status_completed.returncode == 0, status_completed.stderr
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["verification_runs"] == [
        {
            **snapshot["verification_runs"][0],
            "message_id": "B3-msg-cli-status",
            "command": command,
            "status": "succeeded",
            "exit_code": 0,
        }
    ]
    assert snapshot["verification_runs"][0]["stdout_exists"] is True
    assert snapshot["verification_runs"][0]["stderr_exists"] is True

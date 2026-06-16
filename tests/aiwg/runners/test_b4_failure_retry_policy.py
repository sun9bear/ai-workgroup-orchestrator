from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.claims import claim_next_task
from aiwg.state.database import init_database
from aiwg.state.importer import import_inbox, list_tasks

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
    task: str | None = None,
    acceptance: list[str] | None = None,
    max_attempts: int = 2,
    timeout_minutes: int = 30,
    created_at: str = "2026-06-05T12:00:00+08:00",
) -> Path:
    task_id = task or message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-05T120000_from-CodeX_to-Fake_type-instruction_task-{task_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {message_id}",
        f"task: {task_id}",
        "from: CodeX",
        "to: Fake",
        "type: instruction",
        "status: ready",
        "priority: medium",
        'reply_to: ""',
        "requires_human: false",
        f"created_at: {created_at}",
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
            f"max_attempts: {max_attempts}",
            f"timeout_minutes: {timeout_minutes}",
            "review_delegate: CodeX",
            "---",
            "",
            "# B4 failure retry fixture",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def fail_once_then_pass_command(marker: Path) -> str:
    code = (
        "from pathlib import Path; import sys; "
        f"p=Path(r'{marker.as_posix()}'); "
        "exists=p.exists(); p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text('seen', encoding='utf-8'); "
        "print('pass' if exists else 'fail'); "
        "sys.exit(0 if exists else 7)"
    )
    return shell_python_command(code)


def always_fail_command(label: str = "fail") -> str:
    return shell_python_command(f"import sys; print('{label}'); sys.exit(7)")


def db_rows(db_path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def event_types(db_path: Path, message_id: str) -> list[str]:
    return [
        row[0]
        for row in db_rows(
            db_path,
            f"SELECT type FROM events WHERE message_id = '{message_id}' ORDER BY id",
        )
    ]


def test_needs_revision_with_remaining_attempts_is_retried_and_can_finish_done(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    marker = tmp_path / "state" / "retry-marker.txt"
    command = fail_once_then_pass_command(marker)
    write_message(tmp_path, message_id="B4-msg-retry-pass", acceptance=[command], max_attempts=2)

    first = run_once(config=config, project_root=tmp_path, agent="Fake")
    second = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert first.status == "needs_revision"
    assert second.status == "done"
    assert second.message_id == "B4-msg-retry-pass"
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B4-msg-retry-pass", "done", 2)
    ]
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    assert db_rows(db_path, "SELECT status, exit_code FROM verification_runs ORDER BY rowid") == [
        ("failed", 7),
        ("succeeded", 0),
    ]
    assert "task_retry_scheduled" in event_types(db_path, "B4-msg-retry-pass")


def test_verification_failure_at_max_attempts_moves_task_to_waiting_human(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    command = always_fail_command("still-failing")
    write_message(tmp_path, message_id="B4-msg-retry-exhausted", acceptance=[command], max_attempts=2)

    first = run_once(config=config, project_root=tmp_path, agent="Fake")
    second = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert first.status == "needs_revision"
    assert second.status == "waiting_human"
    assert second.message_id == "B4-msg-retry-exhausted"
    assert "exit_code=7" in (second.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B4-msg-retry-exhausted", "waiting_human", 2)
    ]
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    assert db_rows(db_path, "SELECT status, exit_code FROM verification_runs ORDER BY rowid") == [
        ("failed", 7),
        ("failed", 7),
    ]
    assert db_rows(db_path, "SELECT completed_at FROM tasks WHERE id='B4-msg-retry-exhausted'") == [(None,)]
    assert event_types(db_path, "B4-msg-retry-exhausted")[-3:] == [
        "verification_run_started",
        "verification_run_failed",
        "retry_exhausted",
    ]


def test_stale_claim_requires_human_recovery_and_does_not_dispatch_next_ready_task(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        message_id="B4-msg-stale",
        task="B4-stale-first",
        timeout_minutes=1,
        created_at="2026-06-05T11:00:00+08:00",
    )
    write_message(
        tmp_path,
        message_id="B4-msg-ready-behind-stale",
        task="B4-ready-behind-stale",
        created_at="2026-06-05T12:00:00+08:00",
    )
    import_inbox(config=config, project_root=tmp_path, agent="Fake")
    claimed = claim_next_task(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        lock_id="stale-lock",
        now="2026-06-04T00:00:00Z",
    )
    assert claimed is not None
    assert claimed["id"] == "B4-msg-stale"

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "stale_recovery_required"
    assert result.stale_result.staled == 1
    assert result.message_id == "B4-msg-stale"
    assert db_rows(db_path, "SELECT id, status, attempt FROM tasks ORDER BY created_at, id") == [
        ("B4-msg-stale", "stale_claim", 1),
        ("B4-msg-ready-behind-stale", "ready", 0),
    ]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert event_types(db_path, "B4-msg-stale")[-2:] == [
        "claim_marked_stale",
        "stale_recovery_required",
    ]


def test_cli_status_json_exposes_retry_exhaustion_for_human_recovery(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    command = always_fail_command("cli-fail")
    write_message(tmp_path, message_id="B4-msg-cli-exhausted", acceptance=[command], max_attempts=2)

    first = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "Fake"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "Fake"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json", "--recent-events", "20"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "status=needs_revision" in first.stdout
    assert "status=waiting_human" in second.stdout
    assert status_completed.returncode == 0, status_completed.stderr
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"waiting_human": 1}
    assert snapshot["tasks"][0]["id"] == "B4-msg-cli-exhausted"
    assert snapshot["tasks"][0]["status"] == "waiting_human"
    assert snapshot["tasks"][0]["attempt"] == 2
    assert len(snapshot["verification_runs"]) == 2
    assert [run["status"] for run in snapshot["verification_runs"]] == ["failed", "failed"]
    assert snapshot["recent_events"][0]["type"] == "retry_exhausted"
    assert snapshot["recent_events"][0]["payload"]["attempt"] == 2

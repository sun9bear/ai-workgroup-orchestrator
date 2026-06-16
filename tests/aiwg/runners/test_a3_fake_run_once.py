from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.claims import claim_next_task, release_stale_claims
from aiwg.state.database import connect_database, init_database
from aiwg.state.importer import import_inbox, list_tasks

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
    message_id: str = "A3-msg-001",
    task: str = "A3-fake-happy-path",
    status: str = "ready",
    timeout_minutes: int = 30,
) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-04T130000_from-CodeX_to-Fake_type-instruction_task-{task}.md"
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
                f"status: {status}",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-04T13:00:00+08:00",
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
                f"timeout_minutes: {timeout_minutes}",
                "review_delegate: CodeX",
                "---",
                "",
                "# A3 fixture",
                "",
                "用于 Phase A3 Fake adapter happy path。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_claim_next_task_is_atomic_and_logs_event(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="A3-msg-claim")
    import_inbox(config=config, project_root=tmp_path, agent="Fake")

    claimed = claim_next_task(config=config, project_root=tmp_path, agent="Fake", lock_id="lock-a")
    second = claim_next_task(config=config, project_root=tmp_path, agent="Fake", lock_id="lock-b")

    assert claimed is not None
    assert claimed["id"] == "A3-msg-claim"
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "Fake"
    assert claimed["lock_id"] == "lock-a"
    assert claimed["attempt"] == 1
    assert second is None

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status, claimed_by, lock_id, attempt FROM tasks WHERE id = ?", ("A3-msg-claim",)).fetchone() == (
            "claimed",
            "Fake",
            "lock-a",
            1,
        )
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'task_claimed' AND message_id = ?", ("A3-msg-claim",)).fetchone()[0] == 1


def test_release_stale_claims_marks_expired_claims_without_auto_ready(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="A3-msg-stale", timeout_minutes=1)
    import_inbox(config=config, project_root=tmp_path, agent="Fake")
    claimed = claim_next_task(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        lock_id="stale-lock",
        now="2026-06-04T00:00:00Z",
    )
    assert claimed is not None

    result = release_stale_claims(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        now="2026-06-04T00:02:00Z",
    )

    assert result.staled == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", ("A3-msg-stale",)).fetchone()[0] == "stale_claim"
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'claim_marked_stale' AND message_id = ?", ("A3-msg-stale",)).fetchone()[0] == 1


def test_run_once_imports_ready_message_runs_fake_adapter_and_marks_done(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    message_path = write_message(tmp_path, message_id="A3-msg-run-once")

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "done"
    assert result.message_id == "A3-msg-run-once"
    assert result.import_result.imported == 1
    assert result.report_path is not None
    assert result.report_path.exists()
    assert result.report_path.is_relative_to(tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts")
    report = result.report_path.read_text(encoding="utf-8")
    assert "Fake adapter completed task" in report
    assert "A3-msg-run-once" in report
    assert str(message_path.relative_to(tmp_path)).replace("\\", "/") in report

    tasks = list_tasks(config=config, project_root=tmp_path, status="done", agent="Fake")
    assert [task["id"] for task in tasks] == ["A3-msg-run-once"]

    with sqlite3.connect(db_path) as conn:
        event_types = [
            row[0]
            for row in conn.execute(
                "SELECT type FROM events WHERE message_id = ? ORDER BY id",
                ("A3-msg-run-once",),
            )
        ]
        assert event_types == [
            "task_imported",
            "task_claimed",
            "agent_run_started",
            "fake_report_written",
            "agent_run_succeeded",
            "task_done",
        ]
        agent_run = conn.execute(
            "SELECT adapter_type, status, report_path, exit_code FROM agent_runs WHERE message_id = ?",
            ("A3-msg-run-once",),
        ).fetchone()
        assert agent_run[0] == "fake"
        assert agent_run[1] == "succeeded"
        assert agent_run[2]
        assert agent_run[3] == 0


def test_cli_run_once_uses_configured_project_root_and_lists_done_task(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    write_message(tmp_path, message_id="A3-msg-cli")

    run_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "Fake"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    list_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "list-tasks",
            "--config",
            str(config_path),
            "--status",
            "done",
            "--agent",
            "Fake",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert run_completed.returncode == 0, run_completed.stderr
    assert "run-once: agent=Fake status=done message_id=A3-msg-cli" in run_completed.stdout
    assert "report=" in run_completed.stdout
    assert list_completed.returncode == 0, list_completed.stderr
    assert "A3-msg-cli" in list_completed.stdout
    assert "done" in list_completed.stdout

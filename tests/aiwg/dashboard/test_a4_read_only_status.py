from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def write_config(tmp_path: Path, config: dict | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def write_message(project_root: Path, *, message_id: str = "A4-msg-001", task: str = "A4-read-only-status") -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-04T160000_from-CodeX_to-Fake_type-instruction_task-{task}.md"
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
                "created_at: 2026-06-04T16:00:00+08:00",
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
                "# A4 fixture",
                "",
                "用于 Phase A4 只读 status endpoint 测试。",
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


def test_status_snapshot_reads_tasks_events_and_artifacts_without_mutating_db(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="A4-msg-snapshot")
    run_result = run_once(config=config, project_root=tmp_path, agent="Fake")
    assert run_result.status == "done"

    before = db_digest(db_path)
    snapshot = get_status_snapshot(config=config, project_root=tmp_path, recent_events=6)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["capabilities"]["read_only"] is True
    assert snapshot["capabilities"]["mutation_actions"] == []
    assert {"done", "approve", "merge", "cancel", "update_status"}.isdisjoint(
        set(snapshot["capabilities"]["mutation_actions"])
    )
    assert snapshot["database"]["exists"] is True
    assert snapshot["summary"]["total_tasks"] == 1
    assert snapshot["summary"]["status_counts"] == {"done": 1}

    assert len(snapshot["tasks"]) == 1
    task = snapshot["tasks"][0]
    assert task["id"] == "A4-msg-snapshot"
    assert task["status"] == "done"
    assert task["latest_report_path"].endswith("report.md")

    event_types = [event["type"] for event in snapshot["recent_events"]]
    assert event_types == [
        "task_done",
        "agent_run_succeeded",
        "fake_report_written",
        "agent_run_started",
        "task_claimed",
        "task_imported",
    ]

    report_artifacts = [artifact for artifact in snapshot["artifacts"] if artifact["kind"] == "report"]
    assert len(report_artifacts) == 1
    report = report_artifacts[0]
    assert report["message_id"] == "A4-msg-snapshot"
    assert report["exists"] is True
    assert Path(report["path"]).is_relative_to(tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts")


def test_status_snapshot_does_not_create_missing_database(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"

    snapshot = get_status_snapshot(config=config, project_root=tmp_path)

    assert db_path.exists() is False
    assert snapshot["database"]["exists"] is False
    assert snapshot["summary"]["total_tasks"] == 0
    assert snapshot["tasks"] == []
    assert snapshot["recent_events"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["capabilities"]["read_only"] is True


def test_cli_status_json_and_text_are_read_only(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config_path = write_config(tmp_path, config)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="A4-msg-cli")
    assert run_once(config=config, project_root=tmp_path, agent="Fake").status == "done"
    before = db_digest(db_path)

    json_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "status",
            "--config",
            str(config_path),
            "--json",
            "--recent-events",
            "4",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    after = db_digest(db_path)

    assert before == after
    assert json_completed.returncode == 0, json_completed.stderr
    payload = json.loads(json_completed.stdout)
    assert payload["capabilities"]["read_only"] is True
    assert payload["capabilities"]["mutation_actions"] == []
    assert payload["tasks"][0]["id"] == "A4-msg-cli"
    assert payload["tasks"][0]["status"] == "done"
    assert payload["artifacts"][0]["exists"] is True

    assert text_completed.returncode == 0, text_completed.stderr
    assert "AIWG read-only status" in text_completed.stdout
    assert "No mutation actions exposed" in text_completed.stdout
    assert "A4-msg-cli" in text_completed.stdout
    assert "Recent events" in text_completed.stdout
    assert "Artifacts" in text_completed.stdout

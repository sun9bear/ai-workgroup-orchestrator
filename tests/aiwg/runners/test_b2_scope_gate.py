from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from aiwg.config import build_default_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import connect_database, init_database
from aiwg.state.importer import list_tasks


def build_write_enabled_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"]["safe_mode"] = False
    config["policy"]["allow_write"] = True
    return config


def init_git_repo(project_root: Path) -> None:
    subprocess.run(["git", "init"], cwd=project_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(
        ["git", "config", "user.email", "aiwg-tests@example.invalid"],
        cwd=project_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "AIWG Tests"],
        cwd=project_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    (project_root / "allowed").mkdir(parents=True, exist_ok=True)
    (project_root / "outside").mkdir(parents=True, exist_ok=True)
    (project_root / "secrets").mkdir(parents=True, exist_ok=True)
    (project_root / "allowed" / "file.md").write_text("baseline allowed\n", encoding="utf-8")
    (project_root / "outside" / "file.md").write_text("baseline outside\n", encoding="utf-8")
    (project_root / "secrets" / "token.txt").write_text("baseline secret\n", encoding="utf-8")
    subprocess.run(["git", "add", "allowed", "outside", "secrets"], cwd=project_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=project_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def write_message(
    project_root: Path,
    *,
    message_id: str,
    allowed_files: list[str],
    forbidden_files: list[str] | None = None,
) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-05T100000_from-CodeX_to-Fake_type-instruction_task-{message_id}.md"
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
        "created_at: 2026-06-05T10:00:00+08:00",
        "can_write: true",
        "context_files: []",
        "allowed_files:",
    ]
    if allowed_files:
        lines.extend(f"  - {item}" for item in allowed_files)
    else:
        lines[-1] = "allowed_files: []"
    lines.append("forbidden_files:")
    forbidden_files = forbidden_files or [".env"]
    if forbidden_files:
        lines.extend(f"  - {item}" for item in forbidden_files)
    else:
        lines[-1] = "forbidden_files: []"
    lines.extend(
        [
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
            "# B2 fixture",
            "",
            "用于 Phase B2 allowed_files / diff scope gate 测试。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def seed_write_task(
    db_path: Path,
    *,
    message_id: str,
    allowed_files: list[str],
    forbidden_files: list[str] | None = None,
) -> None:
    forbidden_files = forbidden_files or [".env"]
    now = "2026-06-05T10:00:00Z"
    import json

    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks(
              id, task_id, message_path, from_agent, to_agent, type, status, priority,
              requires_human, can_write, worktree_required, max_scope,
              allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
              attempt, max_attempts, timeout_minutes, legacy_imported, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                message_id,
                f"docs/ai-workgroup/inbox/Fake/{message_id}.md",
                "CodeX",
                "Fake",
                "instruction",
                "ready",
                "medium",
                0,
                1,
                0,
                "limited",
                json.dumps(allowed_files),
                json.dumps(forbidden_files),
                "[]",
                "[]",
                0,
                2,
                30,
                0,
                now,
                now,
            ),
        )


def event_rows(db_path: Path) -> list[tuple[str, str | None, str | None, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT type, message_id, status, payload_json FROM events ORDER BY id"
        ).fetchall()


def test_write_task_with_out_of_scope_dirty_diff_is_denied_before_claim_or_dispatch(tmp_path: Path) -> None:
    config = build_write_enabled_config(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "outside" / "file.md").write_text("out of scope edit\n", encoding="utf-8")
    write_message(tmp_path, message_id="B2-msg-outside", allowed_files=["allowed/**"])

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "scope_denied"
    assert result.message_id == "B2-msg-outside"
    assert "outside/file.md" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B2-msg-outside", "waiting_human", 0)
    ]
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    rows = event_rows(db_path)
    assert [row[0] for row in rows] == ["task_imported", "scope_violation"]
    assert rows[-1][1] == "B2-msg-outside"
    assert rows[-1][2] == "waiting_human"
    assert "outside/file.md" in rows[-1][3]


def test_write_task_with_empty_allowed_files_is_denied_before_claim(tmp_path: Path) -> None:
    config = build_write_enabled_config(tmp_path)
    init_git_repo(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    seed_write_task(db_path, message_id="B2-msg-empty-allowed", allowed_files=[])

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "scope_denied"
    assert result.message_id == "B2-msg-empty-allowed"
    assert "allowed_files" in (result.error or "")
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B2-msg-empty-allowed", "waiting_human", 0)
    ]
    rows = event_rows(db_path)
    assert [row[0] for row in rows] == ["scope_violation"]
    assert "allowed_files_required" in rows[-1][3]


def test_write_task_with_forbidden_dirty_diff_is_denied_even_when_allowed_by_broad_pattern(tmp_path: Path) -> None:
    config = build_write_enabled_config(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "secrets" / "token.txt").write_text("secret edit\n", encoding="utf-8")
    db_path = init_database(config=config, project_root=tmp_path)
    seed_write_task(
        db_path,
        message_id="B2-msg-forbidden",
        allowed_files=["**"],
        forbidden_files=["secrets/**"],
    )

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "scope_denied"
    assert result.message_id == "B2-msg-forbidden"
    assert "secrets/token.txt" in (result.error or "")
    rows = event_rows(db_path)
    assert [row[0] for row in rows] == ["scope_violation"]
    assert "forbidden" in rows[-1][3]


def test_write_task_with_in_scope_dirty_diff_can_dispatch_fake_and_records_scope_check(tmp_path: Path) -> None:
    config = build_write_enabled_config(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "allowed" / "file.md").write_text("in scope edit\n", encoding="utf-8")
    write_message(tmp_path, message_id="B2-msg-in-scope", allowed_files=["allowed/**"])

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "done"
    assert result.message_id == "B2-msg-in-scope"
    assert result.report_path is not None
    tasks = list_tasks(config=config, project_root=tmp_path, agent="Fake")
    assert [(task["id"], task["status"], task["attempt"]) for task in tasks] == [
        ("B2-msg-in-scope", "done", 1)
    ]
    rows = event_rows(tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite")
    assert [row[0] for row in rows] == [
        "task_imported",
        "scope_checked",
        "task_claimed",
        "agent_run_started",
        "fake_report_written",
        "agent_run_succeeded",
        "task_done",
    ]
    assert "allowed/file.md" in rows[1][3]


def test_read_only_fake_task_does_not_require_git_scope_gate(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    # No git init here: read-only Fake work must keep the Phase A/A3 behavior.
    path = (
        tmp_path
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / "2026-06-05T103000_from-CodeX_to-Fake_type-instruction_task-B2-read-only.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                "id: B2-msg-read-only",
                "task: B2-read-only",
                "from: CodeX",
                "to: Fake",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-05T10:30:00+08:00",
                "can_write: false",
                "context_files: []",
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
                "# B2 read-only fixture",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "done"
    assert result.message_id == "B2-msg-read-only"

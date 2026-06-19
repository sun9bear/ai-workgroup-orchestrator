from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.claims import claim_next_task
from aiwg.state.database import init_database
from aiwg.state.importer import import_inbox


def build_test_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def write_message(
    project_root: Path,
    *,
    message_id: str,
    task: str | None = None,
    status: str = "ready",
    can_write: bool = False,
    attempt: int = 0,
    max_attempts: int = 2,
    timeout_minutes: int = 30,
    created_at: str = "2026-06-09T12:00:00+08:00",
) -> Path:
    task_id = task or message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-09T120000_from-CodeX_to-Fake_type-instruction_task-{task_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {task_id}",
                "from: CodeX",
                "to: Fake",
                "type: instruction",
                f"status: {status}",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                f"created_at: {created_at}",
                f"can_write: {str(can_write).lower()}",
                "context_files: []",
                "allowed_files:",
                "  - docs/ai-workgroup/working/**" if can_write else "  []",
                "forbidden_files:",
                "  - .env",
                "acceptance: []",
                'claimed_by: ""',
                'claimed_at: ""',
                'lock_id: ""',
                f"attempt: {attempt}",
                f"max_attempts: {max_attempts}",
                f"timeout_minutes: {timeout_minutes}",
                "review_delegate: CodeX",
                "---",
                "",
                "# D5.3.11 runner policy fixture",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def event_count(db_path: Path) -> int:
    return int(db_rows(db_path, "SELECT COUNT(*) FROM events")[0][0])


def assert_config_contract_invalid(result: Any, key_fragment: str) -> None:
    assert result.status == "policy_denied"
    assert result.error == "config_contract_invalid"
    assert any(key_fragment in reason and "literal bool" in reason for reason in result.policy_reasons), result.policy_reasons


def test_d5311_run_once_rejects_non_mapping_policy_before_runner_work(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"] = ["not", "mapping"]
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "policy_denied"
    assert result.error == "config_contract_invalid"
    assert result.import_result.scanned == 0
    assert result.stale_result.staled == 0
    assert any("policy must be a mapping" in reason for reason in result.policy_reasons), result.policy_reasons
    assert not db_path.exists()


def test_d5311_auto_retry_write_tasks_string_false_fails_before_retry_state_mutation(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        message_id="D5311-msg-write-retry",
        task="D5311-write-retry",
        status="needs_revision",
        can_write=True,
        attempt=1,
        max_attempts=2,
    )
    imported = import_inbox(config=config, project_root=tmp_path, agent="Fake")
    assert imported.valid == 1
    before_events = event_count(db_path)
    config["policy"]["auto_retry_write_tasks"] = "false"

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert_config_contract_invalid(result, "policy.auto_retry_write_tasks")
    assert db_rows(db_path, "SELECT status, attempt, can_write FROM tasks WHERE id = ?", ("D5311-msg-write-retry",)) == [
        ("needs_revision", 1, 1)
    ]
    assert event_count(db_path) == before_events
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]


def test_d5311_auto_retry_needs_revision_string_false_fails_before_retry_state_mutation(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        message_id="D5311-msg-needs-revision",
        task="D5311-needs-revision",
        status="needs_revision",
        can_write=False,
        attempt=1,
        max_attempts=2,
    )
    imported = import_inbox(config=config, project_root=tmp_path, agent="Fake")
    assert imported.valid == 1
    before_events = event_count(db_path)
    config["policy"]["auto_retry_needs_revision"] = "false"

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert_config_contract_invalid(result, "policy.auto_retry_needs_revision")
    assert db_rows(db_path, "SELECT status, attempt FROM tasks WHERE id = ?", ("D5311-msg-needs-revision",)) == [
        ("needs_revision", 1)
    ]
    assert event_count(db_path) == before_events
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]


def test_d5311_stale_claim_requires_human_integer_zero_fails_before_stale_release_or_dispatch(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        message_id="D5311-msg-stale",
        task="D5311-stale-first",
        timeout_minutes=1,
        created_at="2026-06-09T11:00:00+08:00",
    )
    write_message(
        tmp_path,
        message_id="D5311-msg-ready-behind-stale",
        task="D5311-ready-behind-stale",
        created_at="2026-06-09T12:00:00+08:00",
    )
    imported = import_inbox(config=config, project_root=tmp_path, agent="Fake")
    assert imported.valid == 2
    claimed = claim_next_task(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        lock_id="d5311-stale-lock",
        now="2026-06-08T00:00:00Z",
    )
    assert claimed is not None
    assert claimed["id"] == "D5311-msg-stale"
    before_events = event_count(db_path)
    config["policy"]["stale_claim_requires_human"] = 0

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert_config_contract_invalid(result, "policy.stale_claim_requires_human")
    assert db_rows(db_path, "SELECT id, status, claimed_by, attempt FROM tasks ORDER BY created_at, id") == [
        ("D5311-msg-stale", "claimed", "Fake", 1),
        ("D5311-msg-ready-behind-stale", "ready", None, 0),
    ]
    assert event_count(db_path) == before_events
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]

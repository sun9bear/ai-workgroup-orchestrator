from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config
from aiwg.runners import orchestrator as orchestrator_module
from aiwg.runners.orchestrator import run_once
from aiwg.state import claims
from aiwg.state.claims import claim_next_task, release_stale_claims
from aiwg.state.database import init_database
from aiwg.state.importer import import_inbox


def build_test_config(tmp_path: Path) -> dict[str, Any]:
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
    safe_task = task_id.replace(":", "-")
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-05T120000_from-CodeX_to-Fake_type-instruction_task-{safe_task}.md"
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
            "# B5 concurrent claim fixture",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def db_fetchall(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def event_count(db_path: Path, message_id: str, event_type: str) -> int:
    return int(
        db_fetchall(
            db_path,
            "SELECT COUNT(*) FROM events WHERE message_id = ? AND type = ?",
            (message_id, event_type),
        )[0][0]
    )


def run_many(config: dict[str, Any], project_root: Path, *, workers: int) -> list[str]:
    statuses: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(run_once, config=config, project_root=project_root, agent="Fake")
            for _ in range(workers)
        ]
        for future in as_completed(futures):
            result = future.result()
            statuses.append(result.status)
    return statuses


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


def test_claim_ready_task_by_id_does_not_fall_through_to_later_ready_task(tmp_path: Path) -> None:
    claim_by_id = getattr(claims, "claim_ready_task_by_id", None)
    assert claim_by_id is not None, "B5 needs a candidate-specific atomic claim helper"

    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="B5-msg-first", created_at="2026-06-05T10:00:00+08:00")
    write_message(tmp_path, message_id="B5-msg-second", created_at="2026-06-05T10:01:00+08:00")
    import_inbox(config=config, project_root=tmp_path, agent="Fake")

    first_claim = claim_by_id(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        message_id="B5-msg-first",
        lock_id="lock-a",
    )
    second_attempt = claim_by_id(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        message_id="B5-msg-first",
        lock_id="lock-b",
    )

    assert first_claim is not None
    assert first_claim["id"] == "B5-msg-first"
    assert second_attempt is None
    assert db_fetchall(db_path, "SELECT id, status, attempt, lock_id FROM tasks ORDER BY created_at, id") == [
        ("B5-msg-first", "claimed", 1, "lock-a"),
        ("B5-msg-second", "ready", 0, None),
    ]
    assert event_count(db_path, "B5-msg-first", "task_claimed") == 1
    assert event_count(db_path, "B5-msg-second", "task_claimed") == 0


def test_run_once_does_not_claim_unchecked_later_task_when_prechecked_candidate_is_lost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="B5-msg-race-first", created_at="2026-06-05T10:00:00+08:00")
    write_message(tmp_path, message_id="B5-msg-race-second", created_at="2026-06-05T10:01:00+08:00")
    import_inbox(config=config, project_root=tmp_path, agent="Fake")

    original_evaluate = orchestrator_module.evaluate_runtime_policy
    stolen = {"done": False}

    def steal_candidate_after_task_policy(*, config, project_root, agent, adapter_type, task=None):
        decision = original_evaluate(
            config=config,
            project_root=project_root,
            agent=agent,
            adapter_type=adapter_type,
            task=task,
        )
        if task is not None and task["id"] == "B5-msg-race-first" and not stolen["done"]:
            stolen["done"] = True
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'claimed', claimed_by = 'Other', claimed_at = '2026-06-05T00:00:00Z',
                        lock_id = 'other-lock', attempt = attempt + 1, updated_at = '2026-06-05T00:00:00Z'
                    WHERE id = ? AND status = 'ready'
                    """,
                    ("B5-msg-race-first",),
                )
        return decision

    monkeypatch.setattr(orchestrator_module, "evaluate_runtime_policy", steal_candidate_after_task_policy)

    result = run_once(config=config, project_root=tmp_path, agent="Fake")

    assert result.status == "idle"
    assert result.message_id is None
    assert db_fetchall(db_path, "SELECT id, status, attempt FROM tasks ORDER BY created_at, id") == [
        ("B5-msg-race-first", "claimed", 1),
        ("B5-msg-race-second", "ready", 0),
    ]
    assert db_fetchall(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert event_count(db_path, "B5-msg-race-second", "task_claimed") == 0


def test_existing_stale_claim_blocks_later_ready_work_and_records_recovery_once(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        message_id="B5-msg-existing-stale",
        created_at="2026-06-05T10:00:00+08:00",
        timeout_minutes=1,
    )
    write_message(
        tmp_path,
        message_id="B5-msg-ready-behind-existing-stale",
        created_at="2026-06-05T10:01:00+08:00",
    )
    import_inbox(config=config, project_root=tmp_path, agent="Fake")
    claimed = claim_next_task(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        lock_id="stale-lock",
        now="2026-06-05T00:00:00Z",
    )
    assert claimed is not None
    stale_result = release_stale_claims(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        now="2026-06-05T00:02:00Z",
    )
    assert stale_result.staled == 1

    statuses = run_many(config, tmp_path, workers=4)

    assert statuses == ["stale_recovery_required"] * 4
    assert db_fetchall(db_path, "SELECT id, status, attempt FROM tasks ORDER BY created_at, id") == [
        ("B5-msg-existing-stale", "stale_claim", 1),
        ("B5-msg-ready-behind-existing-stale", "ready", 0),
    ]
    assert db_fetchall(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert event_count(db_path, "B5-msg-existing-stale", "claim_marked_stale") == 1
    assert event_count(db_path, "B5-msg-existing-stale", "stale_recovery_required") == 1


def test_concurrent_run_once_claims_each_ready_task_at_most_once(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    expected_ids = [f"B5-msg-ready-{index:02d}" for index in range(8)]
    for index, message_id in enumerate(expected_ids):
        write_message(
            tmp_path,
            message_id=message_id,
            created_at=f"2026-06-05T10:{index:02d}:00+08:00",
        )
    import_inbox(config=config, project_root=tmp_path, agent="Fake")

    statuses = run_many(config, tmp_path, workers=12)

    assert statuses.count("done") == len(expected_ids)
    assert statuses.count("idle") == 12 - len(expected_ids)
    assert db_fetchall(db_path, "SELECT status, attempt, COUNT(*) FROM tasks GROUP BY status, attempt") == [
        ("done", 1, len(expected_ids))
    ]
    assert db_fetchall(db_path, "SELECT COUNT(*), COUNT(DISTINCT message_id) FROM agent_runs") == [
        (len(expected_ids), len(expected_ids))
    ]
    assert db_fetchall(
        db_path,
        """
        SELECT COUNT(*)
        FROM (
          SELECT message_id
          FROM events
          WHERE type = 'task_claimed'
          GROUP BY message_id
          HAVING COUNT(*) != 1
        )
        """,
    ) == [(0,)]


def test_concurrent_retry_candidate_is_scheduled_and_claimed_once(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    marker = tmp_path / "state" / "retry-marker.txt"
    command = fail_once_then_pass_command(marker)
    write_message(
        tmp_path,
        message_id="B5-msg-concurrent-retry",
        acceptance=[command],
        max_attempts=2,
    )
    first = run_once(config=config, project_root=tmp_path, agent="Fake")
    assert first.status == "needs_revision"

    statuses = run_many(config, tmp_path, workers=8)

    assert statuses.count("done") == 1
    assert statuses.count("idle") == 7
    assert db_fetchall(db_path, "SELECT status, attempt FROM tasks WHERE id = ?", ("B5-msg-concurrent-retry",)) == [
        ("done", 2)
    ]
    assert db_fetchall(db_path, "SELECT status, exit_code FROM verification_runs ORDER BY rowid") == [
        ("failed", 7),
        ("succeeded", 0),
    ]
    assert event_count(db_path, "B5-msg-concurrent-retry", "task_retry_scheduled") == 1
    assert event_count(db_path, "B5-msg-concurrent-retry", "task_claimed") == 2
    assert db_fetchall(db_path, "SELECT COUNT(*), COUNT(DISTINCT id) FROM agent_runs") == [(2, 2)]

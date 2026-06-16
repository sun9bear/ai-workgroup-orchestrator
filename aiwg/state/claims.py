from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiwg.state.database import connect_database, init_database, resolve_db_path, utc_now_iso


@dataclass(frozen=True)
class StaleClaimResult:
    staled: int = 0
    message_ids: list[str] = field(default_factory=list)


def claim_next_task(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    agent: str,
    lock_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim one ready task for an agent.

    SQLite serializes the SELECT+UPDATE with BEGIN IMMEDIATE, and the UPDATE
    still guards on status/requires_human/attempt so a stale candidate cannot be
    claimed after another worker changes it.
    """

    project_root_path = Path(project_root)
    init_database(config=config, project_root=project_root_path)
    db_path = resolve_db_path(config, project_root_path)
    timestamp = now or utc_now_iso()
    task_lock_id = lock_id or f"claim-{uuid.uuid4().hex}"

    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute(
            """
            SELECT id
            FROM tasks
            WHERE status = 'ready'
              AND to_agent = ?
              AND requires_human = 0
              AND attempt < max_attempts
            ORDER BY created_at, id
            LIMIT 1
            """,
            (agent,),
        ).fetchone()
        if candidate is None:
            return None

        updated = conn.execute(
            """
            UPDATE tasks
            SET status = 'claimed',
                claimed_by = ?,
                claimed_at = ?,
                lock_id = ?,
                attempt = attempt + 1,
                updated_at = ?
            WHERE id = ?
              AND status = 'ready'
              AND requires_human = 0
              AND attempt < max_attempts
            """,
            (agent, timestamp, task_lock_id, timestamp, candidate["id"]),
        )
        if updated.rowcount != 1:
            return None

        row = conn.execute(_TASK_SELECT_SQL + " WHERE id = ?", (candidate["id"],)).fetchone()
        task = _decode_task_row(row)
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="task_claimed",
            status="claimed",
            payload={"lock_id": task_lock_id},
            now=timestamp,
        )
        return task


def claim_ready_task_by_id(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    agent: str,
    message_id: str,
    lock_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim one specific pre-checked ready task for an agent.

    This is used when runtime policy/scope gates evaluated a concrete candidate.
    If another worker claims that candidate before this call, return None rather
    than falling through to a later ready task that has not been checked yet.
    """

    project_root_path = Path(project_root)
    init_database(config=config, project_root=project_root_path)
    db_path = resolve_db_path(config, project_root_path)
    timestamp = now or utc_now_iso()
    task_lock_id = lock_id or f"claim-{uuid.uuid4().hex}"

    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE tasks
            SET status = 'claimed',
                claimed_by = ?,
                claimed_at = ?,
                lock_id = ?,
                attempt = attempt + 1,
                updated_at = ?
            WHERE id = ?
              AND to_agent = ?
              AND status = 'ready'
              AND requires_human = 0
              AND attempt < max_attempts
            """,
            (agent, timestamp, task_lock_id, timestamp, message_id, agent),
        )
        if updated.rowcount != 1:
            return None

        row = conn.execute(_TASK_SELECT_SQL + " WHERE id = ?", (message_id,)).fetchone()
        task = _decode_task_row(row)
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="task_claimed",
            status="claimed",
            payload={"lock_id": task_lock_id},
            now=timestamp,
        )
        return task


def release_stale_claims(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    agent: str | None = None,
    now: str | None = None,
) -> StaleClaimResult:
    """Mark expired claimed/working tasks as stale_claim.

    Phase A3 deliberately does not move stale tasks back to ready automatically;
    later policy/scope phases can decide whether manual recovery is required.
    """

    project_root_path = Path(project_root)
    init_database(config=config, project_root=project_root_path)
    db_path = resolve_db_path(config, project_root_path)
    timestamp = now or utc_now_iso()
    now_dt = _parse_iso8601(timestamp)
    message_ids: list[str] = []

    clauses = ["status IN ('claimed', 'working')", "claimed_at IS NOT NULL", "claimed_at != ''"]
    params: list[Any] = []
    if agent is not None:
        clauses.append("to_agent = ?")
        params.append(agent)
    where = " AND ".join(clauses)

    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(_TASK_SELECT_SQL + f" WHERE {where} ORDER BY claimed_at, id", params).fetchall()
        for row in rows:
            task = _decode_task_row(row)
            claimed_at = _parse_iso8601(str(task["claimed_at"]))
            age_seconds = (now_dt - claimed_at).total_seconds()
            timeout_seconds = int(task["timeout_minutes"]) * 60
            if age_seconds < timeout_seconds:
                continue

            conn.execute(
                """
                UPDATE tasks
                SET status = 'stale_claim', updated_at = ?
                WHERE id = ? AND status IN ('claimed', 'working')
                """,
                (timestamp, task["id"]),
            )
            stale_task = dict(task)
            stale_task["status"] = "stale_claim"
            _insert_event(
                conn,
                task=stale_task,
                agent="Orchestrator",
                event_type="claim_marked_stale",
                status="stale_claim",
                payload={
                    "claimed_by": task.get("claimed_by"),
                    "lock_id": task.get("lock_id"),
                    "age_seconds": int(age_seconds),
                },
                now=timestamp,
            )
            message_ids.append(str(task["id"]))

    return StaleClaimResult(staled=len(message_ids), message_ids=message_ids)


def mark_task_working(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    message_id: str,
    now: str | None = None,
) -> None:
    _update_task_status(config, project_root, message_id=message_id, status="working", now=now)


def mark_task_done(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    task: dict[str, Any],
    agent: str,
    report_path: Path | None,
    now: str | None = None,
) -> None:
    timestamp = now or utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'done', updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, task["id"]),
        )
        done_task = dict(task)
        done_task["status"] = "done"
        _insert_event(
            conn,
            task=done_task,
            agent=agent,
            event_type="task_done",
            status="done",
            payload={"report_path": str(report_path) if report_path is not None else None},
            now=timestamp,
        )


def mark_task_failed(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    task: dict[str, Any],
    agent: str,
    error: str,
    now: str | None = None,
) -> None:
    timestamp = now or utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'failed', updated_at = ? WHERE id = ?",
            (timestamp, task["id"]),
        )
        failed_task = dict(task)
        failed_task["status"] = "failed"
        _insert_event(
            conn,
            task=failed_task,
            agent=agent,
            event_type="task_failed",
            status="failed",
            payload={"error": error},
            now=timestamp,
        )


def _update_task_status(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    message_id: str,
    status: str,
    now: str | None = None,
) -> None:
    timestamp = now or utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, timestamp, message_id),
        )


_TASK_SELECT_SQL = """
SELECT id, task_id, message_path, from_agent, to_agent, type, status, priority,
       requires_human, can_write, worktree_required, max_scope, review_delegate,
       allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
       claimed_by, claimed_at, lock_id, attempt, max_attempts, timeout_minutes,
       created_at, updated_at, completed_at
FROM tasks
"""


def _decode_task_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for bool_field in ("requires_human", "can_write", "worktree_required"):
        result[bool_field] = bool(result[bool_field])
    result["allowed_files"] = json.loads(result.pop("allowed_files_json"))
    result["forbidden_files"] = json.loads(result.pop("forbidden_files_json"))
    result["context_files"] = json.loads(result.pop("context_files_json"))
    result["acceptance"] = json.loads(result.pop("acceptance_json"))
    return result


def _insert_event(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    status: str | None,
    payload: dict[str, Any],
    now: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(task["task_id"]),
            str(task["id"]),
            agent,
            event_type,
            status,
            str(task.get("message_path") or ""),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now or utc_now_iso(),
        ),
    )


def _parse_iso8601(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiwg.adapter_registry import write_restricted_adapter_preflight
from aiwg.policy import evaluate_runtime_policy
from aiwg.runners.fake import AdapterRunResult, FakeAdapter
from aiwg.scope import ScopeDecision, evaluate_scope_gate
from aiwg.state.claims import (
    StaleClaimResult,
    claim_ready_task_by_id,
    mark_task_done,
    mark_task_failed,
    mark_task_working,
    release_stale_claims,
)
from aiwg.state.database import connect_database, init_database, resolve_db_path, utc_now_iso
from aiwg.state.importer import ImportResult, import_inbox
from aiwg.verification import VerificationOutcome, run_verification_commands


@dataclass(frozen=True)
class RetryPreparationResult:
    status: str
    message_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RunOnceResult:
    agent: str
    status: str
    message_id: str | None
    import_result: ImportResult
    stale_result: StaleClaimResult
    report_path: Path | None = None
    manifest_path: Path | None = None
    error: str | None = None
    policy_reasons: list[str] = field(default_factory=list)


def run_once(config: dict[str, Any], project_root: Path | str, *, agent: str) -> RunOnceResult:
    """Run one Phase A3 orchestrator tick for a single agent.

    The tick imports new inbox messages, marks expired claims stale, atomically
    claims one ready task, and dispatches it to the configured adapter. Phase A3
    only supports the local Fake adapter; real adapters remain disabled until
    later policy-gate phases.
    """

    project_root_path = Path(project_root)
    init_database(config=config, project_root=project_root_path)
    import_result = import_inbox(config=config, project_root=project_root_path, agent=agent)
    stale_result = release_stale_claims(config=config, project_root=project_root_path, agent=agent)
    if _policy_bool(config, "stale_claim_requires_human", default=True):
        stale_message_id = _record_unresolved_stale_recovery_required(
            config=config,
            project_root=project_root_path,
            agent=agent,
        )
        if stale_message_id is not None:
            return RunOnceResult(
                agent=agent,
                status="stale_recovery_required",
                message_id=stale_message_id,
                import_result=import_result,
                stale_result=stale_result,
                error="stale_claim_requires_human",
            )

    agent_config = (config.get("agents") or {}).get(agent) or {}
    adapter_type = str(agent_config.get("adapter") or "")

    agent_policy = evaluate_runtime_policy(
        config=config,
        project_root=project_root_path,
        agent=agent,
        adapter_type=adapter_type,
    )
    if not agent_policy.allowed:
        _record_policy_denied(
            config=config,
            project_root=project_root_path,
            agent=agent,
            task=None,
            reasons=agent_policy.reasons,
            status="policy_denied",
        )
        return RunOnceResult(
            agent=agent,
            status="policy_denied",
            message_id=None,
            import_result=import_result,
            stale_result=stale_result,
            error=agent_policy.error,
            policy_reasons=agent_policy.reasons,
        )

    retry_preparation = _prepare_retry_candidate(config=config, project_root=project_root_path, agent=agent)
    if retry_preparation.status in {"waiting_human", "retry_blocked"}:
        return RunOnceResult(
            agent=agent,
            status=retry_preparation.status,
            message_id=retry_preparation.message_id,
            import_result=import_result,
            stale_result=stale_result,
            error=retry_preparation.error,
        )

    candidate_task = _peek_next_ready_task(config=config, project_root=project_root_path, agent=agent)
    if candidate_task is None:
        return RunOnceResult(
            agent=agent,
            status="idle",
            message_id=None,
            import_result=import_result,
            stale_result=stale_result,
        )

    task_policy = evaluate_runtime_policy(
        config=config,
        project_root=project_root_path,
        agent=agent,
        adapter_type=adapter_type,
        task=candidate_task,
    )
    if not task_policy.allowed:
        _mark_task_waiting_human_due_to_policy(
            config=config,
            project_root=project_root_path,
            task=candidate_task,
            agent=agent,
            reasons=task_policy.reasons,
        )
        return RunOnceResult(
            agent=agent,
            status="policy_denied",
            message_id=str(candidate_task["id"]),
            import_result=import_result,
            stale_result=stale_result,
            error=task_policy.error,
            policy_reasons=task_policy.reasons,
        )

    scope_decision = evaluate_scope_gate(config=config, project_root=project_root_path, task=candidate_task)
    if scope_decision.applies:
        if not scope_decision.allowed:
            _mark_task_waiting_human_due_to_scope(
                config=config,
                project_root=project_root_path,
                task=candidate_task,
                agent=agent,
                scope_decision=scope_decision,
            )
            return RunOnceResult(
                agent=agent,
                status="scope_denied",
                message_id=str(candidate_task["id"]),
                import_result=import_result,
                stale_result=stale_result,
                error=scope_decision.error,
                policy_reasons=scope_decision.reasons,
            )
        _record_scope_checked(
            config=config,
            project_root=project_root_path,
            task=candidate_task,
            agent=agent,
            scope_decision=scope_decision,
        )

    if adapter_type != "fake":
        preflight_artifact = _mark_task_waiting_human_due_to_adapter_preflight(
            config=config,
            project_root=project_root_path,
            task=candidate_task,
            agent=agent,
            adapter_type=adapter_type,
        )
        return RunOnceResult(
            agent=agent,
            status="adapter_preflight_required",
            message_id=str(candidate_task["id"]),
            import_result=import_result,
            stale_result=stale_result,
            manifest_path=preflight_artifact.manifest_path,
            error="real_adapter_dispatch_not_implemented_in_b6",
        )

    task = claim_ready_task_by_id(
        config=config,
        project_root=project_root_path,
        agent=agent,
        message_id=str(candidate_task["id"]),
        lock_id=f"run-once-{uuid4().hex}",
    )
    if task is None:
        return RunOnceResult(
            agent=agent,
            status="idle",
            message_id=None,
            import_result=import_result,
            stale_result=stale_result,
        )

    run_id = f"run-{uuid4().hex}"
    mark_task_working(config=config, project_root=project_root_path, message_id=str(task["id"]))
    _start_agent_run(
        config=config,
        project_root=project_root_path,
        run_id=run_id,
        task=task,
        agent=agent,
        adapter_type=adapter_type,
    )

    adapter = FakeAdapter()
    try:
        adapter_result = adapter.run(task=task, config=config, project_root=project_root_path)
        _record_fake_report_written(
            config=config,
            project_root=project_root_path,
            task=task,
            agent=agent,
            adapter_result=adapter_result,
        )
        _finish_agent_run(
            config=config,
            project_root=project_root_path,
            run_id=run_id,
            task=task,
            agent=agent,
            adapter_result=adapter_result,
        )
        verification_outcome = run_verification_commands(
            config=config,
            project_root=project_root_path,
            task=task,
            agent=agent,
        )
        if not verification_outcome.passed:
            failure_status = _mark_task_after_verification_failure(
                config=config,
                project_root=project_root_path,
                task=task,
                agent=agent,
                verification_outcome=verification_outcome,
            )
            return RunOnceResult(
                agent=agent,
                status=failure_status,
                message_id=str(task["id"]),
                import_result=import_result,
                stale_result=stale_result,
                report_path=adapter_result.report_path,
                error=verification_outcome.error,
            )
        mark_task_done(
            config=config,
            project_root=project_root_path,
            task=task,
            agent=agent,
            report_path=adapter_result.report_path,
        )
        return RunOnceResult(
            agent=agent,
            status="done",
            message_id=str(task["id"]),
            import_result=import_result,
            stale_result=stale_result,
            report_path=adapter_result.report_path,
        )
    except Exception as exc:  # pragma: no cover - defensive boundary for future adapters
        error = str(exc)
        _fail_agent_run(
            config=config,
            project_root=project_root_path,
            run_id=run_id,
            task=task,
            agent=agent,
            error=error,
        )
        mark_task_failed(config=config, project_root=project_root_path, task=task, agent=agent, error=error)
        return RunOnceResult(
            agent=agent,
            status="failed",
            message_id=str(task["id"]),
            import_result=import_result,
            stale_result=stale_result,
            error=error,
        )


_TASK_POLICY_SELECT_SQL = """
SELECT id, task_id, message_path, from_agent, to_agent, type, status, priority,
       requires_human, can_write, worktree_required, max_scope, review_delegate,
       allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
       claimed_by, claimed_at, lock_id, attempt, max_attempts, timeout_minutes,
       created_at, updated_at, completed_at
FROM tasks
"""


def _policy_bool(config: dict[str, Any], key: str, *, default: bool) -> bool:
    policy = config.get("policy") or {}
    if key not in policy:
        return default
    return bool(policy.get(key))


def _record_unresolved_stale_recovery_required(
    *,
    config: dict[str, Any],
    project_root: Path,
    agent: str,
) -> str | None:
    db_path = resolve_db_path(config, project_root)
    now = utc_now_iso()
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            _TASK_POLICY_SELECT_SQL
            + """
            WHERE status = 'stale_claim'
              AND to_agent = ?
            ORDER BY updated_at, created_at, id
            LIMIT 1
            """,
            (agent,),
        ).fetchone()
        if row is None:
            return None
        task = _decode_task_row(row)
        already_recorded = conn.execute(
            """
            SELECT 1
            FROM events
            WHERE message_id = ? AND type = 'stale_recovery_required'
            LIMIT 1
            """,
            (str(task["id"]),),
        ).fetchone()
        if already_recorded is None:
            _insert_event(
                conn,
                task=task,
                agent=agent,
                event_type="stale_recovery_required",
                status="stale_claim",
                payload={
                    "reason": "stale_claim_requires_human",
                    "claimed_by": task.get("claimed_by"),
                    "lock_id": task.get("lock_id"),
                },
                now=now,
            )
        return str(task["id"])


def _prepare_retry_candidate(
    *,
    config: dict[str, Any],
    project_root: Path,
    agent: str,
) -> RetryPreparationResult:
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            _TASK_POLICY_SELECT_SQL
            + """
            WHERE status = 'needs_revision'
              AND to_agent = ?
            ORDER BY updated_at, created_at, id
            LIMIT 1
            """,
            (agent,),
        ).fetchone()
        if row is None:
            return RetryPreparationResult(status="none")
        task = _decode_task_row(row)
        now = utc_now_iso()
        attempt = int(task["attempt"])
        max_attempts = int(task["max_attempts"])

        if not _policy_bool(config, "auto_retry_needs_revision", default=True):
            return _move_retry_task_to_waiting_human(
                conn,
                task=task,
                agent=agent,
                now=now,
                event_type="retry_blocked",
                error="auto_retry_needs_revision_disabled",
                payload={"reason": "auto_retry_needs_revision_disabled", "attempt": attempt, "max_attempts": max_attempts},
            )
        if bool(task.get("requires_human")):
            return _move_retry_task_to_waiting_human(
                conn,
                task=task,
                agent=agent,
                now=now,
                event_type="retry_blocked",
                error="requires_human",
                payload={"reason": "requires_human", "attempt": attempt, "max_attempts": max_attempts},
            )
        if bool(task.get("can_write")) and not _policy_bool(config, "auto_retry_write_tasks", default=False):
            return _move_retry_task_to_waiting_human(
                conn,
                task=task,
                agent=agent,
                now=now,
                event_type="retry_blocked",
                error="write_task_retry_requires_human",
                payload={"reason": "write_task_retry_requires_human", "attempt": attempt, "max_attempts": max_attempts},
            )
        if attempt >= max_attempts:
            return _move_retry_task_to_waiting_human(
                conn,
                task=task,
                agent=agent,
                now=now,
                event_type="retry_exhausted",
                error=f"retry_exhausted: attempt={attempt} max_attempts={max_attempts}",
                payload={"reason": "max_attempts_reached", "attempt": attempt, "max_attempts": max_attempts},
            )

        conn.execute(
            """
            UPDATE tasks
            SET status = 'ready', claimed_by = NULL, claimed_at = NULL, lock_id = NULL, updated_at = ?
            WHERE id = ? AND status = 'needs_revision' AND attempt < max_attempts
            """,
            (now, str(task["id"])),
        )
        ready_task = dict(task)
        ready_task["status"] = "ready"
        _insert_event(
            conn,
            task=ready_task,
            agent="Orchestrator",
            event_type="task_retry_scheduled",
            status="ready",
            payload={"reason": "needs_revision", "attempt": attempt, "max_attempts": max_attempts},
            now=now,
        )
        return RetryPreparationResult(status="scheduled", message_id=str(task["id"]))


def _move_retry_task_to_waiting_human(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any],
    agent: str,
    now: str,
    event_type: str,
    error: str,
    payload: dict[str, Any],
) -> RetryPreparationResult:
    conn.execute(
        """
        UPDATE tasks
        SET status = 'waiting_human', claimed_by = NULL, claimed_at = NULL, lock_id = NULL,
            updated_at = ?
        WHERE id = ? AND status = 'needs_revision'
        """,
        (now, str(task["id"])),
    )
    waiting_task = dict(task)
    waiting_task["status"] = "waiting_human"
    _insert_event(
        conn,
        task=waiting_task,
        agent=agent,
        event_type=event_type,
        status="waiting_human",
        payload=payload,
        now=now,
    )
    return RetryPreparationResult(status="waiting_human", message_id=str(task["id"]), error=error)


def _peek_next_ready_task(
    *,
    config: dict[str, Any],
    project_root: Path,
    agent: str,
) -> dict[str, Any] | None:
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            _TASK_POLICY_SELECT_SQL
            + """
            WHERE status = 'ready'
              AND to_agent = ?
              AND attempt < max_attempts
            ORDER BY created_at, id
            LIMIT 1
            """,
            (agent,),
        ).fetchone()
    return _decode_task_row(row) if row is not None else None


def _decode_task_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for bool_field in ("requires_human", "can_write", "worktree_required"):
        result[bool_field] = bool(result[bool_field])
    result["allowed_files"] = json.loads(result.pop("allowed_files_json"))
    result["forbidden_files"] = json.loads(result.pop("forbidden_files_json"))
    result["context_files"] = json.loads(result.pop("context_files_json"))
    result["acceptance"] = json.loads(result.pop("acceptance_json"))
    return result


def _mark_task_waiting_human_due_to_policy(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    reasons: list[str],
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'waiting_human', updated_at = ?
            WHERE id = ? AND status = 'ready'
            """,
            (now, str(task["id"])),
        )
        denied_task = dict(task)
        denied_task["status"] = "waiting_human"
        _insert_policy_denied_event(
            conn,
            task=denied_task,
            agent=agent,
            status="waiting_human",
            reasons=reasons,
            now=now,
        )


def _record_policy_denied(
    *,
    config: dict[str, Any],
    project_root: Path,
    agent: str,
    task: dict[str, Any] | None,
    reasons: list[str],
    status: str,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        _insert_policy_denied_event(
            conn,
            task=task,
            agent=agent,
            status=status,
            reasons=reasons,
            now=now,
        )


def _insert_policy_denied_event(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any] | None,
    agent: str,
    status: str,
    reasons: list[str],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(task["task_id"]) if task is not None else None,
            str(task["id"]) if task is not None else None,
            agent,
            "policy_denied",
            status,
            str(task.get("message_path") or "") if task is not None else None,
            json.dumps({"reasons": reasons}, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )


def _mark_task_waiting_human_due_to_adapter_preflight(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
):
    preflight_artifact = write_restricted_adapter_preflight(
        config=config,
        project_root=project_root,
        agent=agent,
        adapter_type=adapter_type,
        task=task,
    )
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'waiting_human', updated_at = ?
            WHERE id = ? AND status = 'ready'
            """,
            (now, str(task["id"])),
        )
        waiting_task = dict(task)
        waiting_task["status"] = "waiting_human"
        _insert_event(
            conn,
            task=waiting_task,
            agent=agent,
            event_type="adapter_preflight_required",
            status="waiting_human",
            payload={
                "reason": "real_adapter_dispatch_not_implemented_in_b6",
                "adapter_type": adapter_type,
                "manifest_path": str(preflight_artifact.manifest_path),
                "prompt_path": str(preflight_artifact.prompt_path),
                "dispatch_allowed": bool(preflight_artifact.manifest.get("dispatch_allowed")),
                "forbidden_side_effects": list(preflight_artifact.manifest.get("forbidden_side_effects") or []),
            },
            now=now,
        )
    return preflight_artifact


def _mark_task_waiting_human_due_to_scope(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    scope_decision: ScopeDecision,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'waiting_human', updated_at = ?
            WHERE id = ? AND status = 'ready'
            """,
            (now, str(task["id"])),
        )
        denied_task = dict(task)
        denied_task["status"] = "waiting_human"
        _insert_event(
            conn,
            task=denied_task,
            agent=agent,
            event_type="scope_violation",
            status="waiting_human",
            payload=scope_decision.payload(),
            now=now,
        )


def _record_scope_checked(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    scope_decision: ScopeDecision,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="scope_checked",
            status="ready",
            payload=scope_decision.payload(),
            now=now,
        )


def _mark_task_after_verification_failure(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    verification_outcome: VerificationOutcome,
) -> str:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    attempt = int(task["attempt"])
    max_attempts = int(task["max_attempts"])
    verification_run_ids = [result.id for result in verification_outcome.results]
    if attempt >= max_attempts:
        with connect_database(db_path) as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'waiting_human', updated_at = ?
                WHERE id = ?
                """,
                (now, str(task["id"])),
            )
            waiting_task = dict(task)
            waiting_task["status"] = "waiting_human"
            _insert_event(
                conn,
                task=waiting_task,
                agent=agent,
                event_type="retry_exhausted",
                status="waiting_human",
                payload={
                    "reason": "verification_failed",
                    "error": verification_outcome.error,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "verification_run_ids": verification_run_ids,
                },
                now=now,
            )
        return "waiting_human"

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'needs_revision', updated_at = ?
            WHERE id = ?
            """,
            (now, str(task["id"])),
        )
        revision_task = dict(task)
        revision_task["status"] = "needs_revision"
        _insert_event(
            conn,
            task=revision_task,
            agent=agent,
            event_type="task_needs_revision",
            status="needs_revision",
            payload={
                "reason": "verification_failed",
                "error": verification_outcome.error,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "verification_run_ids": verification_run_ids,
            },
            now=now,
        )
    return "needs_revision"


def _start_agent_run(
    *,
    config: dict[str, Any],
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO agent_runs(
              id, message_id, agent, adapter_type, status, started_at,
              timeout_seconds, max_budget_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(task["id"]),
                agent,
                adapter_type,
                "running",
                now,
                int(task["timeout_minutes"]) * 60,
                task.get("max_budget_usd"),
            ),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="agent_run_started",
            status="working",
            payload={"run_id": run_id, "adapter_type": adapter_type},
            now=now,
        )


def _record_fake_report_written(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    adapter_result: AdapterRunResult,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="fake_report_written",
            status="reported",
            payload={"report_path": str(adapter_result.report_path)},
            now=now,
        )


def _finish_agent_run(
    *,
    config: dict[str, Any],
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    agent: str,
    adapter_result: AdapterRunResult,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, finished_at = ?, stdout_path = ?, stderr_path = ?,
                report_path = ?, exit_code = ?, error = ?
            WHERE id = ?
            """,
            (
                adapter_result.status,
                now,
                str(adapter_result.stdout_path),
                str(adapter_result.stderr_path),
                str(adapter_result.report_path),
                adapter_result.exit_code,
                adapter_result.error,
                run_id,
            ),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="agent_run_succeeded",
            status="reported",
            payload={"run_id": run_id, "exit_code": adapter_result.exit_code},
            now=now,
        )


def _fail_agent_run(
    *,
    config: dict[str, Any],
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    agent: str,
    error: str,
) -> None:
    now = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE agent_runs
            SET status = 'failed', finished_at = ?, exit_code = ?, error = ?
            WHERE id = ?
            """,
            (now, 1, error, run_id),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="agent_run_failed",
            status="failed",
            payload={"run_id": run_id, "error": error},
            now=now,
        )


def _insert_event(
    conn,
    *,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    status: str | None,
    payload: dict[str, Any],
    now: str,
) -> None:
    import json

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
            now,
        ),
    )

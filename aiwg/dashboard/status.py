from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiwg.d5_preflight import D5_1_COMPONENTS, D5_1_PREFLIGHT_PHASE, D5_1_PREFLIGHT_SCOPE, row_to_d5_preflight_snapshot
from aiwg.external_review_gate import get_external_review_gate_snapshot
from aiwg.role_health import get_role_health_snapshot
from aiwg.state.database import resolve_db_path, utc_now_iso
from aiwg.workflow_contract import get_workflow_contract_snapshot

READ_ONLY_CAPABILITIES = {
    "read_only": True,
    "mutation_actions": [],
    "note": "A4 status/dashboard endpoint only reads SQLite and exposes no done/approve/merge actions.",
}

ADAPTER_READINESS_STALE_WARNING = {
    "code": "adapter_readiness_stale",
    "severity": "warning",
    "message": (
        "Adapter readiness is stale and runtime-only; do not use it to authorize real agent "
        "startup. Re-run adapter-readiness and real-mode preflight before any real agent start."
    ),
    "action": "rerun_adapter_readiness_and_preflight_before_real_agent_start",
    "blocks_real_agent_start": True,
    "read_only": True,
}


def get_status_snapshot(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    recent_events: int = 10,
    task_limit: int = 50,
    status: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Return a read-only dashboard/status snapshot from SQLite.

    This function intentionally does not call init_database() or connect_database():
    A4 status must not create files, switch journal modes, claim tasks, or mutate
    business state. Existing databases are opened with SQLite URI mode=ro.
    """

    project_root_path = Path(project_root)
    db_path = resolve_db_path(config, project_root_path)
    snapshot = _empty_snapshot(db_path=db_path)
    snapshot["role_health"] = get_role_health_snapshot(
        config=config,
        project_root=project_root_path,
        generated_at=str(snapshot["generated_at"]),
    )
    snapshot["external_review_gate"] = get_external_review_gate_snapshot(
        config=config,
        project_root=project_root_path,
        generated_at=str(snapshot["generated_at"]),
    )
    snapshot["workflow_contract"] = get_workflow_contract_snapshot(
        config=config,
        project_root=project_root_path,
        generated_at=str(snapshot["generated_at"]),
    )
    if not db_path.exists():
        return snapshot

    with _connect_readonly(db_path) as conn:
        snapshot["summary"] = _read_summary(conn)
        snapshot["tasks"] = _read_tasks(
            conn,
            status=status,
            agent=agent,
            limit=max(1, int(task_limit)),
        )
        snapshot["recent_events"] = _read_recent_events(conn, limit=max(1, int(recent_events)))
        snapshot["agent_runs"] = _read_agent_runs(conn, limit=max(1, int(task_limit)))
        snapshot["verification_runs"] = _read_verification_runs(conn, limit=max(1, int(task_limit)))
        snapshot["operator_approvals"] = _read_operator_approvals(conn, limit=max(1, int(task_limit)))
        snapshot["adapter_readiness"] = _read_latest_adapter_readiness(
            conn,
            config=config,
            generated_at=str(snapshot["generated_at"]),
        )
        if snapshot["adapter_readiness"] and snapshot["adapter_readiness"].get("stale"):
            warning = dict(ADAPTER_READINESS_STALE_WARNING)
            snapshot["adapter_readiness"]["warning"] = warning
            snapshot["warnings"].append(warning)
        snapshot["latest_real_start_authorization"] = _read_latest_real_start_authorization(
            conn,
            generated_at=str(snapshot["generated_at"]),
        )
        snapshot["latest_real_mode_preflight"] = _read_latest_real_mode_preflight(conn)
        snapshot["d5_preflight"] = _read_latest_d5_preflight(conn)
        snapshot["artifacts"] = _artifact_links(snapshot["agent_runs"], project_root=project_root_path)
    return snapshot


def render_status_text(snapshot: dict[str, Any]) -> str:
    lines = [
        "AIWG read-only status",
        f"generated_at: {snapshot['generated_at']}",
        f"database: {snapshot['database']['path']}",
        f"database_exists: {str(bool(snapshot['database']['exists'])).lower()}",
        "capabilities: read_only=true; No mutation actions exposed",
        "",
        "Tasks by status",
    ]

    warnings = snapshot.get("warnings") or []
    if warnings:
        lines.extend(["", "Warnings"])
        for warning in warnings:
            lines.append(
                f"- [{warning.get('severity')}] {warning.get('code')}: {warning.get('message')} "
                f"action={warning.get('action')} "
                f"blocks_real_agent_start={str(bool(warning.get('blocks_real_agent_start'))).lower()} "
                f"read_only={str(bool(warning.get('read_only'))).lower()}"
            )

    status_counts = snapshot.get("summary", {}).get("status_counts", {})
    if status_counts:
        for status, count in status_counts.items():
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "Tasks"])
    tasks = snapshot.get("tasks") or []
    if tasks:
        for task in tasks:
            report = task.get("latest_report_path") or "-"
            lines.append(
                f"- {task['id']} | {task['task_id']} | {task['status']} | "
                f"to={task['to_agent']} | report={report}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Recent events"])
    events = snapshot.get("recent_events") or []
    if events:
        for event in events:
            lines.append(
                f"- #{event['id']} {event['type']} | message={event['message_id']} | "
                f"status={event.get('status') or '-'} | agent={event['agent']}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Verification runs"])
    verification_runs = snapshot.get("verification_runs") or []
    if verification_runs:
        for run in verification_runs:
            lines.append(
                f"- {run['id']} | message={run['message_id']} | status={run['status']} | "
                f"exit={run.get('exit_code')} | command={run['command']}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Operator approvals"])
    operator_approvals = snapshot.get("operator_approvals") or []
    if operator_approvals:
        for approval in operator_approvals:
            lines.append(
                f"- {approval['id']} | message={approval['message_id']} | decision={approval['decision']} | "
                f"operator={approval['operator']} | expires={approval['expires_at']}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Adapter readiness"])
    adapter_readiness = snapshot.get("adapter_readiness")
    if adapter_readiness:
        summary = adapter_readiness.get("summary") or {}
        lines.append(
            f"- report={adapter_readiness.get('report_path')} | "
            f"available={summary.get('available', 0)} | missing={summary.get('missing', 0)} | "
            f"checked_at={adapter_readiness.get('checked_at')} | "
            f"stale={str(bool(adapter_readiness.get('stale'))).lower()}"
        )
    else:
        lines.append("- none")

    lines.extend(["", "Real-start authorization"])
    real_start = snapshot.get("latest_real_start_authorization")
    if real_start:
        lines.append(
            f"- message={real_start.get('message_id')} | agent={real_start.get('agent')} | "
            f"status={real_start.get('status')} | approval={real_start.get('approval_id')} | "
            f"expires={real_start.get('expires_at')} | "
            f"revoked={str(bool(real_start.get('revoked'))).lower()} | "
            f"real_authorized={str(bool(real_start.get('real_execution_authorized'))).lower()}"
        )
        if real_start.get("authorization_path"):
            lines.append(f"  authorization={real_start.get('authorization_path')}")
    else:
        lines.append("- none")

    lines.extend(["", "Real-mode preflight"])
    real_preflight = snapshot.get("latest_real_mode_preflight")
    if real_preflight:
        lines.append(
            f"- message={real_preflight.get('message_id')} | agent={real_preflight.get('agent')} | "
            f"reason={real_preflight.get('blocked_reason')} | "
            f"chain_verified={str(bool(real_preflight.get('preflight_chain_verified'))).lower()} | "
            f"real_start_auth_verified={str(bool(real_preflight.get('real_start_authorization_verified'))).lower()} | "
            f"real_authorized={str(bool(real_preflight.get('real_execution_authorized'))).lower()} | "
            f"started_real_process={str(bool(real_preflight.get('started_real_process'))).lower()}"
        )
        if real_preflight.get("sandbox_plan_path"):
            lines.append(f"  plan={real_preflight.get('sandbox_plan_path')}")
        if real_preflight.get("sandbox_process_report_path"):
            lines.append(f"  probe_report={real_preflight.get('sandbox_process_report_path')}")
        if real_preflight.get("real_start_authorization_path"):
            lines.append(f"  real_start_authorization={real_preflight.get('real_start_authorization_path')}")
    else:
        lines.append("- none")

    lines.extend(["", "Role health"])
    role_health = snapshot.get("role_health") or {}
    role_cards = (role_health.get("dashboard") or {}).get("cards") or []
    if role_cards:
        for card in role_cards:
            lines.append(
                f"- {card.get('role')} | status={card.get('status')} | "
                f"reason={card.get('reason') or '-'} | "
                f"current_task={card.get('current_task_id') or '-'}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "External review gate"])
    external_review = snapshot.get("external_review_gate") or {}
    if external_review:
        summary = external_review.get("items_summary") or {}
        lines.append(
            f"- status={external_review.get('gate_state')} | "
            f"sources={(external_review.get('sources_summary') or {}).get('source_count', 0)} | "
            f"items={summary.get('item_count', 0)} | "
            f"unresolved_actionable={summary.get('unresolved_actionable_count', 0)} | "
            f"pr_comment_performed={str(bool(external_review.get('pr_comment_performed'))).lower()}"
        )
    else:
        lines.append("- none")

    lines.extend(["", "Workflow contract"])
    workflow_contract = snapshot.get("workflow_contract") or {}
    if workflow_contract:
        workflow_summary = workflow_contract.get("summary") or {}
        validation = workflow_contract.get("validation") or {}
        lines.append(
            f"- validation_passed={str(bool(validation.get('passed'))).lower()} | "
            f"roles={workflow_summary.get('role_count', 0)} | "
            f"checkpoints={workflow_summary.get('checkpoint_count', 0)} | "
            f"read_only={str(bool(workflow_contract.get('read_only'))).lower()} | "
            f"mutation_actions={workflow_contract.get('mutation_actions') or []}"
        )
    else:
        lines.append("- none")

    lines.extend(["", "D5 preflight"])
    d5_preflight = snapshot.get("d5_preflight") or {}
    if d5_preflight:
        budget = d5_preflight.get("budget_preflight") or {}
        checkpoint_lease = d5_preflight.get("checkpoint_lease_preflight") or {}
        fixture = d5_preflight.get("external_review_fixture_ingest") or {}
        d5_line = (
            f"- status={d5_preflight.get('status')} | "
            f"scope={d5_preflight.get('d5_scope')} | "
            f"dry_run={str(bool(d5_preflight.get('dry_run'))).lower()} | "
            f"fake_only={str(bool(d5_preflight.get('fake_only'))).lower()} | "
            f"ready_for_real_agent_execution={str(bool(d5_preflight.get('ready_for_real_agent_execution'))).lower()} | "
            f"target_writes_performed={str(bool(d5_preflight.get('target_writes_performed'))).lower()} | "
            f"mcp_mutation_tools_exposed={str(bool(d5_preflight.get('mcp_mutation_tools_exposed'))).lower()}"
        )
        if budget:
            d5_line += f" | budget={budget.get('status')}"
        if checkpoint_lease:
            d5_line += f" | checkpoint_lease={checkpoint_lease.get('status')}"
        if fixture:
            d5_line += f" | external_review_fixture={fixture.get('gate_state')}"
        lines.append(d5_line)
    else:
        lines.append("- none")

    lines.extend(["", "Artifacts"])
    artifacts = snapshot.get("artifacts") or []
    if artifacts:
        for artifact in artifacts:
            exists = "exists" if artifact["exists"] else "missing"
            lines.append(
                f"- {artifact['kind']} | message={artifact['message_id']} | {exists} | {artifact['path']}"
            )
    else:
        lines.append("- none")

    lines.append("")
    return "\n".join(lines)


def _empty_snapshot(db_path: Path) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "database": {
            "path": str(db_path),
            "exists": db_path.exists(),
            "mode": "read_only",
        },
        "capabilities": dict(READ_ONLY_CAPABILITIES),
        "summary": {
            "total_tasks": 0,
            "status_counts": {},
        },
        "warnings": [],
        "tasks": [],
        "recent_events": [],
        "agent_runs": [],
        "verification_runs": [],
        "operator_approvals": [],
        "adapter_readiness": None,
        "latest_real_start_authorization": None,
        "latest_real_mode_preflight": None,
        "role_health": None,
        "external_review_gate": None,
        "workflow_contract": None,
        "d5_preflight": None,
        "artifacts": [],
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
    ).fetchall()
    status_counts = {str(row["status"]): int(row["count"]) for row in rows}
    return {
        "total_tasks": sum(status_counts.values()),
        "status_counts": status_counts,
    }


def _read_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None,
    agent: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    if agent:
        clauses.append("t.to_agent = ?")
        params.append(agent)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
          t.id, t.task_id, t.status, t.from_agent, t.to_agent, t.priority,
          t.requires_human, t.can_write, t.message_path, t.claimed_by,
          t.attempt, t.max_attempts, t.created_at, t.updated_at, t.completed_at,
          (
            SELECT ar.report_path
            FROM agent_runs ar
            WHERE ar.message_id = t.id AND ar.report_path IS NOT NULL AND ar.report_path != ''
            ORDER BY COALESCE(ar.finished_at, ar.started_at) DESC, ar.id DESC
            LIMIT 1
          ) AS latest_report_path,
          (
            SELECT ar.status
            FROM agent_runs ar
            WHERE ar.message_id = t.id
            ORDER BY COALESCE(ar.finished_at, ar.started_at) DESC, ar.id DESC
            LIMIT 1
          ) AS latest_run_status
        FROM tasks t
        {where}
        ORDER BY t.updated_at DESC, t.id
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [_dict_with_bools(row, bool_fields={"requires_human", "can_write"}) for row in rows]


def _read_recent_events(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, task_id, message_id, agent, type, status, path, command,
               exit_code, duration_ms, payload_json, created_at
        FROM events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["payload"] = _parse_json_object(event.pop("payload_json"))
        events.append(event)
    return events


def _read_agent_runs(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, message_id, agent, adapter_type, status, started_at, finished_at,
               stdout_path, stderr_path, report_path, exit_code, error
        FROM agent_runs
        ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _read_verification_runs(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, message_id, command, cwd, status, started_at, finished_at,
               duration_ms, exit_code, stdout_path, stderr_path
        FROM verification_runs
        ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    runs: list[dict[str, Any]] = []
    for row in rows:
        run = dict(row)
        stdout_path = Path(str(run["stdout_path"])) if run.get("stdout_path") else None
        stderr_path = Path(str(run["stderr_path"])) if run.get("stderr_path") else None
        run["stdout_exists"] = bool(stdout_path and stdout_path.exists())
        run["stderr_exists"] = bool(stderr_path and stderr_path.exists())
        runs.append(run)
    return runs


def _read_operator_approvals(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "operator_approvals"):
        return []
    rows = conn.execute(
        """
        SELECT id, message_id, agent, adapter_type, decision, operator, reason,
               manifest_path, manifest_sha256, expires_at, created_at, used_at
        FROM operator_approvals
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _read_latest_adapter_readiness(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    generated_at: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT path, payload_json, created_at
        FROM events
        WHERE type = 'adapter_binary_readiness_checked'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    payload = _parse_json_object(row["payload_json"])
    checked_at = str(row["created_at"])
    max_age_minutes = _adapter_readiness_max_age_minutes(config)
    age_seconds = _age_seconds(checked_at=checked_at, generated_at=generated_at)
    stale = bool(age_seconds is not None and age_seconds > max_age_minutes * 60)
    return {
        "report_path": payload.get("report_path") or row["path"],
        "summary": payload.get("summary") or {},
        "created_at": checked_at,
        "checked_at": checked_at,
        "age_seconds": age_seconds,
        "max_age_minutes": max_age_minutes,
        "stale": stale,
        "stale_reason": "adapter_readiness_report_stale" if stale else None,
        "started_version_probe_process": bool(payload.get("started_version_probe_process", False)),
        "started_real_agent_task_process": bool(payload.get("started_real_agent_task_process", False)),
        "started_adapter_process": bool(payload.get("started_adapter_process", False)),
        "auto_install": bool(payload.get("auto_install", False)),
        "auto_login": bool(payload.get("auto_login", False)),
        "read_tokens": bool(payload.get("read_tokens", False)),
    }


def _read_latest_real_start_authorization(
    conn: sqlite3.Connection,
    *,
    generated_at: str,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT id, task_id, message_id, agent, type, status, path, payload_json, created_at
        FROM events
        WHERE type IN ('real_start_authorization_approved', 'real_start_authorization_revoked')
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()
    now_dt = _parse_iso_datetime(generated_at)
    for row in rows:
        payload = _parse_json_object(row["payload_json"])
        authorization_path = payload.get("authorization_path") or payload.get("real_start_authorization_path")
        expires_at = payload.get("expires_at") or payload.get("real_start_authorization_expires_at")
        expires_dt = _parse_iso_datetime(str(expires_at)) if expires_at else None
        expired = bool(now_dt is not None and expires_dt is not None and expires_dt <= now_dt)
        revoked = row["type"] == "real_start_authorization_revoked" or bool(payload.get("revoked", False))
        status = "revoked" if revoked else ("expired" if expired else "authorized")
        return {
            "event_id": int(row["id"]),
            "created_at": row["created_at"],
            "task_id": row["task_id"],
            "message_id": row["message_id"],
            "agent": row["agent"],
            "status": status,
            "approval_id": payload.get("approval_id"),
            "authorization_path": authorization_path,
            "authorization_schema_version": payload.get("authorization_schema_version") or payload.get("schema_version"),
            "expires_at": expires_at,
            "expired": expired,
            "revoked": revoked,
            "revoked_at": payload.get("revoked_at"),
            "revoked_by": payload.get("revoked_by"),
            "revocation_reason": payload.get("revocation_reason") or payload.get("reason"),
            "preflight_chain_verified": bool(payload.get("preflight_chain_verified", False)),
            "real_start_authorization_written": bool(payload.get("real_start_authorization_written", False)),
            "real_execution_authorized": bool(payload.get("real_execution_authorized", False)),
            "started_real_process": bool(payload.get("started_real_process", False)),
            "real_agent_binary_started": bool(payload.get("real_agent_binary_started", False)),
            "secret_values_recorded": bool(payload.get("secret_values_recorded", False)),
            "codex_automation_locked": bool(payload.get("codex_automation_locked", False)),
        }
    return None


def _read_latest_real_mode_preflight(conn: sqlite3.Connection) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT id, task_id, message_id, agent, status, path, payload_json, created_at
        FROM events
        WHERE type = 'preflight_resume_blocked'
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()
    for row in rows:
        payload = _parse_json_object(row["payload_json"])
        if payload.get("phase") != "B16-real-mode-launch-preflight":
            continue
        binding = payload.get("readiness_binding") if isinstance(payload.get("readiness_binding"), dict) else {}
        codex_lock = binding.get("codex_automation_lock") if isinstance(binding.get("codex_automation_lock"), dict) else {}
        return {
            "event_id": int(row["id"]),
            "created_at": row["created_at"],
            "task_id": row["task_id"],
            "message_id": row["message_id"],
            "agent": row["agent"],
            "status": "blocked",
            "phase": payload.get("phase"),
            "blocked_reason": payload.get("reason"),
            "approval_id": payload.get("approval_id"),
            "adapter_type": payload.get("adapter_type"),
            "requires_successful_probe": bool(payload.get("requires_successful_probe", False)),
            "requires_real_start_authorization": bool(payload.get("requires_real_start_authorization", False)),
            "preflight_chain_verified": bool(payload.get("preflight_chain_verified", False)),
            "real_start_authorization_path": payload.get("real_start_authorization_path") or None,
            "real_start_authorization_verified": bool(payload.get("real_start_authorization_verified", False)),
            "explicit_real_start_authorized": bool(payload.get("explicit_real_start_authorized", False)),
            "real_start_authorization_operator": payload.get("real_start_authorization_operator") or None,
            "real_start_authorization_expires_at": payload.get("real_start_authorization_expires_at") or None,
            "real_start_authorization_revoked": bool(payload.get("real_start_authorization_revoked", False)),
            "real_start_authorization_revoked_at": payload.get("real_start_authorization_revoked_at") or None,
            "real_start_authorization_revoked_by": payload.get("real_start_authorization_revoked_by") or None,
            "sandbox_plan_path": payload.get("sandbox_plan_path") or None,
            "sandbox_process_report_path": payload.get("probe_report_path") or None,
            "probe_run_id": payload.get("probe_run_id") or None,
            "probe_status": payload.get("probe_status") or None,
            "sandbox_plan_schema_version": payload.get("sandbox_plan_schema_version") or None,
            "sandbox_process_report_schema_version": payload.get("sandbox_process_report_schema_version") or None,
            "readiness_event_id": payload.get("readiness_event_id") or payload.get("current_readiness_event_id"),
            "readiness_report_path": payload.get("readiness_report_path") or None,
            "adapter_binary_resolved_path": payload.get("adapter_binary_resolved_path") or payload.get("current_resolved_path") or None,
            "readiness_binding_bound": bool(binding.get("bound", False)),
            "binary_path_verified": bool(binding.get("binary_path_verified", False)),
            "started_real_process": bool(payload.get("started_real_process", False)),
            "real_agent_binary_started": bool(payload.get("real_agent_binary_started", False)),
            "real_execution_authorized": bool(payload.get("real_execution_authorized", False)),
            "codex_automation_locked": bool(
                payload.get("codex_automation_locked", False)
                or binding.get("codex_automation_locked", False)
                or codex_lock.get("codex_automation_locked", False)
            ),
            "secret_values_recorded": bool(payload.get("secret_values_recorded", False)),
        }
    return None


def _read_latest_d5_preflight(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not _table_exists(conn, "d5_preflight_runs"):
        return None
    row = conn.execute(
        """
        SELECT *
        FROM d5_preflight_runs
        ORDER BY updated_at DESC, created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    snapshot = row_to_d5_preflight_snapshot(row)
    run_id = str(snapshot["preflight_run_id"])
    if _table_exists(conn, "d5_artifact_provenance"):
        provenance = conn.execute(
            """
            SELECT id, preflight_run_id, artifact_kind, artifact_path, artifact_sha256,
                   origin_component, workflow_id, step_id, intent_id,
                   under_orchestrator_root, under_target_root, created_at
            FROM d5_artifact_provenance
            WHERE preflight_run_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if provenance is not None:
            provenance_dict = dict(provenance)
            provenance_dict["under_orchestrator_root"] = bool(provenance_dict["under_orchestrator_root"])
            provenance_dict["under_target_root"] = bool(provenance_dict["under_target_root"])
            snapshot["artifact_provenance"] = provenance_dict

    budget = _read_d5_budget_preflight(conn, run_id=run_id)
    lease = _read_d5_checkpoint_lease_preflight(conn, run_id=run_id)
    fixture = _read_d5_external_review_fixture_ingest(conn, run_id=run_id)
    if budget or lease or fixture:
        snapshot["phase"] = D5_1_PREFLIGHT_PHASE
        snapshot["d5_scope"] = D5_1_PREFLIGHT_SCOPE
        snapshot["d5_1_components"] = list(D5_1_COMPONENTS)
        snapshot["deferred_to_d5_1"] = []
        snapshot["d5_1_not_implemented"] = False
    if budget:
        snapshot["budget_preflight"] = budget
    if lease:
        snapshot["checkpoint_lease_preflight"] = lease
    if fixture:
        snapshot["external_review_fixture_ingest"] = fixture
    return snapshot


def _read_d5_budget_preflight(conn: sqlite3.Connection, *, run_id: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "d5_budget_preflight"):
        return None
    rows = conn.execute(
        """
        SELECT role, workflow_id, max_budget_usd, requested_budget_usd,
               consumed_budget_usd, status, dry_run, created_at
        FROM d5_budget_preflight
        WHERE preflight_run_id = ?
        ORDER BY role
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return None
    role_rows = []
    for row in rows:
        item = dict(row)
        item["dry_run"] = bool(item.get("dry_run"))
        role_rows.append(item)
    statuses = {str(row.get("status")) for row in role_rows}
    status = "budget_exceeded" if "budget_exceeded" in statuses else "within_budget"
    return {
        "status": status,
        "dry_run": True,
        "fake_only": True,
        "max_budget_usd": max(float(row.get("max_budget_usd") or 0) for row in role_rows),
        "total_requested_budget_usd": sum(float(row.get("requested_budget_usd") or 0) for row in role_rows),
        "total_consumed_budget_usd": sum(float(row.get("consumed_budget_usd") or 0) for row in role_rows),
        "roles": role_rows,
    }


def _read_d5_checkpoint_lease_preflight(conn: sqlite3.Connection, *, run_id: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "d5_checkpoint_lease_preflight"):
        return None
    rows = conn.execute(
        """
        SELECT workflow_id, checkpoint_id, role, lease_state, real_lock_acquired,
               stale_recovery_performed, reset_to_ready_performed,
               heartbeat_expected_seconds, stale_after_seconds, created_at
        FROM d5_checkpoint_lease_preflight
        WHERE preflight_run_id = ?
        ORDER BY checkpoint_id
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        return None
    checkpoints = []
    for row in rows:
        item = dict(row)
        item["real_lock_acquired"] = bool(item.get("real_lock_acquired"))
        item["stale_recovery_performed"] = bool(item.get("stale_recovery_performed"))
        item["reset_to_ready_performed"] = bool(item.get("reset_to_ready_performed"))
        checkpoints.append(item)
    return {
        "status": "checked",
        "checkpoint_count": len(checkpoints),
        "real_lock_acquired": any(bool(row.get("real_lock_acquired")) for row in checkpoints),
        "stale_recovery_performed": any(bool(row.get("stale_recovery_performed")) for row in checkpoints),
        "reset_to_ready_performed": any(bool(row.get("reset_to_ready_performed")) for row in checkpoints),
        "heartbeat_expected_seconds": int(checkpoints[0].get("heartbeat_expected_seconds") or 0),
        "stale_after_seconds": int(checkpoints[0].get("stale_after_seconds") or 0),
        "checkpoints": checkpoints,
    }


def _read_d5_external_review_fixture_ingest(conn: sqlite3.Connection, *, run_id: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "d5_external_review_fixture_ingest"):
        return None
    columns = _table_columns(conn, "d5_external_review_fixture_ingest")
    declared_column = (
        "fixture_declared_read_only"
        if "fixture_declared_read_only" in columns
        else "read_only AS fixture_declared_read_only"
    )
    row = conn.execute(
        f"""
        SELECT fixture_path, fixture_sha256, source_count, item_count, status,
               gate_state, read_only, {declared_column}, mutation_action_count, mutation_actions_json,
               github_write_api_called, pr_comment_performed, pr_mutation_performed,
               created_fix_tasks, target_writes_performed, codex_automation_modified,
               created_at
        FROM d5_external_review_fixture_ingest
        WHERE preflight_run_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["read_only"] = bool(result.get("read_only"))
    result["fixture_declared_read_only"] = bool(result.get("fixture_declared_read_only", True))
    result["mutation_actions"] = _parse_json_array(result.pop("mutation_actions_json", None))
    for field in (
        "github_write_api_called",
        "pr_comment_performed",
        "pr_mutation_performed",
        "created_fix_tasks",
        "target_writes_performed",
        "codex_automation_modified",
    ):
        result[field] = bool(result.get(field))
    return result


def _parse_json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _adapter_readiness_max_age_minutes(config: dict[str, Any]) -> int:
    gate = config.get("adapter_readiness_gate") if isinstance(config.get("adapter_readiness_gate"), dict) else {}
    try:
        value = int(gate.get("max_age_minutes", 60))
    except (TypeError, ValueError):
        value = 60
    return max(1, value)


def _age_seconds(*, checked_at: str, generated_at: str) -> int | None:
    checked = _parse_iso_datetime(checked_at)
    generated = _parse_iso_datetime(generated_at)
    if checked is None or generated is None:
        return None
    return max(0, int((generated - checked).total_seconds()))


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _artifact_links(agent_runs: list[dict[str, Any]], *, project_root: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for run in agent_runs:
        for kind, key in (("report", "report_path"), ("stdout", "stdout_path"), ("stderr", "stderr_path")):
            raw_path = run.get(key)
            if not raw_path:
                continue
            path = Path(str(raw_path))
            artifacts.append(
                {
                    "kind": kind,
                    "message_id": run["message_id"],
                    "agent": run["agent"],
                    "adapter_type": run["adapter_type"],
                    "path": str(path),
                    "relative_path": _relative_or_absolute(path, project_root),
                    "exists": path.exists(),
                }
            )
    return artifacts


def _dict_with_bools(row: sqlite3.Row, *, bool_fields: set[str]) -> dict[str, Any]:
    result = dict(row)
    for field in bool_fields:
        result[field] = bool(result[field])
    return result


def _parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _relative_or_absolute(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)

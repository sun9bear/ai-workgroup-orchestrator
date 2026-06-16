from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiwg.state.database import resolve_db_path, utc_now_iso

ROLE_HEALTH_SCHEMA_VERSION = "aiwg.role_health.v1"
ROLE_HEALTH_STATUSES = (
    "healthy",
    "idle",
    "stale",
    "blocked",
    "failed",
    "unknown",
    "disabled",
    "waiting_human",
    "waiting_peer",
    "queue_empty",
)
ROLE_HEALTH_REASONS = (
    "no_recent_heartbeat",
    "ready_task_unconsumed",
    "claimed_task_stale",
    "failed_task_present",
    "human_gate_present",
    "runner_disabled",
    "scheduler_disabled",
    "queue_empty",
    "reviewer_pending",
    "git_steward_pending",
    "recent_heartbeat",
)

ROLE_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "role": "tech_lead_planner",
        "display_name": "Tech Lead / Planner",
        "agents": ("CodeX", "Codex"),
        "adapter_types": ("codex_cli", "human_review"),
    },
    {
        "role": "reviewer",
        "display_name": "Reviewer",
        "agents": ("Reviewer", "CodeX", "Codex"),
        "adapter_types": ("codex_review", "human_review"),
    },
    {
        "role": "git_steward",
        "display_name": "Git Steward",
        "agents": (),
        "adapter_types": ("git_steward_dry_run",),
        "component": True,
    },
    {
        "role": "claude_implementer",
        "display_name": "Claude Implementer",
        "agents": ("Claude-Code", "Claude"),
        "adapter_types": ("claude_code",),
    },
    {
        "role": "advisor_runner",
        "display_name": "Advisor Runner",
        "agents": ("Hermes", "OpenCode", "Fake"),
        "adapter_types": ("hermes_bridge", "opencode", "fake"),
    },
)

ACTIVE_STATUSES = {"claimed", "working"}
READY_STATUSES = {"ready"}
FAILED_STATUSES = {"failed", "stale_claim", "needs_manual_recovery"}
HUMAN_GATE_STATUSES = {"waiting_human"}
REVIEW_PENDING_STATUSES = {"needs_review", "reviewing"}
TERMINAL_WORKFLOW_STATUSES = {"completed", "done", "cancelled", "archived", "duplicate_idempotency_key"}
PENDING_GIT_GATE_STATES = {
    "planned",
    "branch_proposed",
    "commit_proposed",
    "pr_proposed",
    "pr_not_created_dry_run",
    "ci_pending",
    "review_changes_requested",
    "review_threads_unresolved",
    "ready_for_merge_proposal",
}


def get_role_health_snapshot(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic read-only role-health snapshot.

    D4.2 intentionally reads existing SQLite state with URI ``mode=ro``. It does
    not initialize/migrate a missing database, claim tasks, write heartbeat rows,
    create dashboard artifacts, or call any external agent/runtime.
    """

    project_root_path = Path(project_root)
    db_path = resolve_db_path(config, project_root_path)
    generated_at = generated_at or utc_now_iso()
    thresholds = _thresholds(config)
    snapshot = _empty_snapshot(db_path=db_path, generated_at=generated_at, thresholds=thresholds)
    if not db_path.exists():
        snapshot["roles"] = [_role_without_database(contract) for contract in ROLE_CONTRACTS]
        snapshot["dashboard"] = _dashboard_cards(snapshot["roles"])
        return snapshot

    with _connect_readonly(db_path) as conn:
        table_names = _table_names(conn)
        agent_states = _read_agent_states(conn) if "agent_states" in table_names else {}
        tasks = _read_tasks(conn) if "tasks" in table_names else []
        workflow_observations = _read_workflow_observations(conn, table_names=table_names)
        git_gate_observations = _read_git_gate_observations(conn, table_names=table_names)

    roles = [
        _evaluate_role(
            contract,
            config=config,
            agent_state=agent_states.get(str(contract["role"])),
            tasks=tasks,
            workflow_observations=workflow_observations,
            git_gate_observations=git_gate_observations,
            generated_at=generated_at,
            thresholds=thresholds,
        )
        for contract in ROLE_CONTRACTS
    ]
    blockers = _collect_blockers(roles=roles, tasks=tasks)
    snapshot.update(
        {
            "roles": roles,
            "blockers": blockers,
            "queue_observations": _queue_observations(tasks=tasks, generated_at=generated_at, thresholds=thresholds),
            "review_observations": _review_observations(tasks),
            "workflow_observations": workflow_observations,
            "git_gate_observations": git_gate_observations,
            "current_blocking_classification": _blocking_classification(blockers),
        }
    )
    snapshot["dashboard"] = _dashboard_cards(roles)
    return snapshot


def render_role_health_text(snapshot: dict[str, Any]) -> str:
    lines = [
        "Role health",
        f"generated_at: {snapshot.get('generated_at')}",
        f"database: {(snapshot.get('database') or {}).get('path')}",
        "capabilities: read_only=true; mutation_actions=[]",
        f"current_blocking_classification: {snapshot.get('current_blocking_classification')}",
        "",
        "Role cards",
    ]
    roles = snapshot.get("roles") or []
    if roles:
        for role in roles:
            lines.append(
                f"- {role.get('role')} | status={role.get('status')} | "
                f"reason={role.get('primary_reason') or '-'} | "
                f"ready={role.get('ready_task_count', 0)} | "
                f"claimed_stale={role.get('claimed_stale_count', 0)} | "
                f"current_task={role.get('current_task_id') or '-'}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "Blockers"])
    blockers = snapshot.get("blockers") or []
    if blockers:
        for blocker in blockers:
            lines.append(
                f"- role={blocker.get('role')} reason={blocker.get('reason')} "
                f"status={blocker.get('status')} count={blocker.get('count')} "
                f"next={blocker.get('next_action_role')}"
            )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _empty_snapshot(db_path: Path, generated_at: str, thresholds: dict[str, int]) -> dict[str, Any]:
    return {
        "schema_version": ROLE_HEALTH_SCHEMA_VERSION,
        "generated_at": generated_at,
        "database": {
            "path": str(db_path),
            "exists": db_path.exists(),
            "mode": "read_only",
        },
        "read_only": True,
        "mutation_actions": [],
        "role_health_statuses": list(ROLE_HEALTH_STATUSES),
        "health_reasons": list(ROLE_HEALTH_REASONS),
        "thresholds": dict(thresholds),
        "roles": [],
        "blockers": [],
        "queue_observations": {
            "total_task_count": 0,
            "ready_task_count": 0,
            "stale_ready_task_count": 0,
            "claimed_task_count": 0,
            "claimed_stale_count": 0,
            "failed_task_count": 0,
            "human_gate_count": 0,
        },
        "review_observations": {"pending_review_count": 0},
        "workflow_observations": {"pending_workflow_count": 0, "statuses": {}},
        "git_gate_observations": {"pending_git_gate_count": 0, "gate_states": {}},
        "current_blocking_classification": "unknown" if not db_path.exists() else "clear",
        "dashboard": {"cards": [], "auto_repair_actions": []},
        "ready_for_real_agent_execution": False,
        "ready_for_protected_business_repository_write": False,
        "mcp_mutation_tools_exposed": False,
        "target_writes_performed": False,
        "codex_automation_modified": False,
    }


def _role_without_database(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": contract["role"],
        "display_name": contract["display_name"],
        "agents": list(contract.get("agents") or ()),
        "adapter_types": list(contract.get("adapter_types") or ()),
        "enabled": False,
        "status": "unknown",
        "primary_reason": None,
        "reasons": [],
        "last_seen_at": None,
        "heartbeat_age_seconds": None,
        "current_task_id": None,
        "ready_task_count": 0,
        "oldest_ready_task_age_seconds": None,
        "claimed_task_count": 0,
        "claimed_stale_count": 0,
        "failed_task_count": 0,
        "human_gate_count": 0,
        "next_action_role": contract["role"],
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _read_agent_states(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT role, display_name, adapter_type, enabled, health_status, health_reason,
               last_seen_at, current_task_id, detail_json, updated_at
        FROM agent_states
        """
    ).fetchall()
    states: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = dict(row)
        state["enabled"] = bool(state.get("enabled"))
        state["detail"] = _parse_json_object(state.pop("detail_json", None))
        states[str(state["role"])] = state
    return states


def _read_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, task_id, to_agent, from_agent, status, requires_human,
               claimed_by, claimed_at, updated_at, created_at
        FROM tasks
        """
    ).fetchall()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        task = dict(row)
        task["requires_human"] = bool(task.get("requires_human"))
        tasks.append(task)
    return tasks


def _read_workflow_observations(conn: sqlite3.Connection, *, table_names: set[str]) -> dict[str, Any]:
    if "workflow_runs" not in table_names:
        return {"pending_workflow_count": 0, "statuses": {}}
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM workflow_runs GROUP BY status ORDER BY status").fetchall()
    statuses = {str(row["status"]): int(row["count"]) for row in rows}
    pending = sum(count for status, count in statuses.items() if status not in TERMINAL_WORKFLOW_STATUSES)
    return {"pending_workflow_count": pending, "statuses": statuses}


def _read_git_gate_observations(conn: sqlite3.Connection, *, table_names: set[str]) -> dict[str, Any]:
    if "pr_gate_status" not in table_names:
        return {"pending_git_gate_count": 0, "gate_states": {}}
    rows = conn.execute("SELECT gate_state, COUNT(*) AS count FROM pr_gate_status GROUP BY gate_state ORDER BY gate_state").fetchall()
    states = {str(row["gate_state"]): int(row["count"]) for row in rows}
    pending = sum(count for state, count in states.items() if state in PENDING_GIT_GATE_STATES)
    return {"pending_git_gate_count": pending, "gate_states": states}


def _evaluate_role(
    contract: dict[str, Any],
    *,
    config: dict[str, Any],
    agent_state: dict[str, Any] | None,
    tasks: list[dict[str, Any]],
    workflow_observations: dict[str, Any],
    git_gate_observations: dict[str, Any],
    generated_at: str,
    thresholds: dict[str, int],
) -> dict[str, Any]:
    role = str(contract["role"])
    role_tasks = _tasks_for_role(contract, tasks)
    ready_tasks = [task for task in role_tasks if str(task.get("status")) in READY_STATUSES]
    stale_ready = [
        task
        for task in ready_tasks
        if _task_age_seconds(task, generated_at=generated_at) >= thresholds["ready_task_stale_seconds"]
    ]
    claimed_tasks = [task for task in role_tasks if str(task.get("status")) in ACTIVE_STATUSES]
    stale_claimed = [
        task
        for task in claimed_tasks
        if _claimed_age_seconds(task, generated_at=generated_at) >= thresholds["claimed_task_stale_seconds"]
    ]
    failed_tasks = [task for task in role_tasks if str(task.get("status")) in FAILED_STATUSES]
    human_tasks = [
        task
        for task in role_tasks
        if str(task.get("status")) in HUMAN_GATE_STATUSES or bool(task.get("requires_human"))
    ]
    review_pending = [task for task in tasks if str(task.get("status")) in REVIEW_PENDING_STATUSES]
    config_enabled = _role_config_enabled(config, contract)
    heartbeat_age_seconds = _heartbeat_age_seconds(agent_state, generated_at=generated_at)
    reasons: list[str] = []
    status = "queue_empty"
    primary_reason: str | None = "queue_empty"
    current_task_id = None
    last_seen_at = None
    enabled = config_enabled

    if agent_state is not None:
        enabled = bool(agent_state.get("enabled"))
        current_task_id = agent_state.get("current_task_id")
        last_seen_at = agent_state.get("last_seen_at")

    if failed_tasks:
        status, primary_reason = "failed", "failed_task_present"
    elif human_tasks:
        status, primary_reason = "waiting_human", "human_gate_present"
    elif stale_claimed:
        status, primary_reason = "stale", "claimed_task_stale"
    elif stale_ready:
        status, primary_reason = "stale", "ready_task_unconsumed"
    elif role == "reviewer" and review_pending:
        status, primary_reason = "waiting_peer", "reviewer_pending"
    elif role == "git_steward" and int(git_gate_observations.get("pending_git_gate_count") or 0) > 0:
        status, primary_reason = "waiting_peer", "git_steward_pending"
    elif agent_state is not None and not enabled:
        status, primary_reason = "disabled", "runner_disabled"
    elif agent_state is not None and heartbeat_age_seconds is not None and heartbeat_age_seconds >= thresholds["heartbeat_stale_seconds"]:
        status, primary_reason = "stale", "no_recent_heartbeat"
    elif agent_state is not None:
        state_status = str(agent_state.get("health_status") or "unknown")
        status = state_status if state_status in ROLE_HEALTH_STATUSES else "unknown"
        state_reason = agent_state.get("health_reason")
        if state_reason:
            candidate_reason = str(state_reason)
            primary_reason = (
                candidate_reason
                if candidate_reason in ROLE_HEALTH_REASONS
                else "recent_heartbeat" if status == "healthy" else None
            )
        else:
            primary_reason = "recent_heartbeat" if status == "healthy" else "no_recent_heartbeat"
    elif not config_enabled and role != "git_steward":
        status, primary_reason = "disabled", "runner_disabled"
    elif claimed_tasks:
        status, primary_reason = "healthy", "recent_heartbeat"
        current_task_id = str(claimed_tasks[0].get("id") or claimed_tasks[0].get("task_id") or "") or None
    elif ready_tasks:
        status, primary_reason = "idle", None
    elif int(workflow_observations.get("pending_workflow_count") or 0) > 0 and role == "tech_lead_planner":
        status, primary_reason = "waiting_peer", "scheduler_disabled"
    else:
        status, primary_reason = "queue_empty", "queue_empty"

    for candidate in (
        "failed_task_present" if failed_tasks else None,
        "human_gate_present" if human_tasks else None,
        "claimed_task_stale" if stale_claimed else None,
        "ready_task_unconsumed" if stale_ready else None,
        "reviewer_pending" if role == "reviewer" and review_pending else None,
        "git_steward_pending" if role == "git_steward" and int(git_gate_observations.get("pending_git_gate_count") or 0) > 0 else None,
        "no_recent_heartbeat" if agent_state is not None and heartbeat_age_seconds is not None and heartbeat_age_seconds >= thresholds["heartbeat_stale_seconds"] else None,
        "runner_disabled" if not enabled and role != "git_steward" else None,
        "queue_empty" if not role_tasks and role != "git_steward" else None,
    ):
        if candidate and candidate not in reasons:
            reasons.append(candidate)

    oldest_ready_age = None
    if ready_tasks:
        oldest_ready_age = max(_task_age_seconds(task, generated_at=generated_at) for task in ready_tasks)

    return {
        "role": role,
        "display_name": str(agent_state.get("display_name") if agent_state else contract["display_name"]),
        "agents": list(contract.get("agents") or ()),
        "adapter_types": list(contract.get("adapter_types") or ()),
        "enabled": bool(enabled),
        "status": status,
        "primary_reason": primary_reason,
        "reasons": reasons,
        "last_seen_at": last_seen_at,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "current_task_id": current_task_id,
        "ready_task_count": len(ready_tasks),
        "oldest_ready_task_age_seconds": oldest_ready_age,
        "claimed_task_count": len(claimed_tasks),
        "claimed_stale_count": len(stale_claimed),
        "failed_task_count": len(failed_tasks),
        "human_gate_count": len(human_tasks),
        "review_pending_count": len(review_pending) if role == "reviewer" else 0,
        "git_gate_pending_count": int(git_gate_observations.get("pending_git_gate_count") or 0) if role == "git_steward" else 0,
        "next_action_role": _next_action_role(role=role, reason=primary_reason),
    }


def _tasks_for_role(contract: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents = {str(agent) for agent in contract.get("agents") or ()}
    if not agents:
        return []
    return [task for task in tasks if str(task.get("to_agent")) in agents]


def _role_config_enabled(config: dict[str, Any], contract: dict[str, Any]) -> bool:
    if bool(contract.get("component")):
        return True
    agents_config = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    for agent in contract.get("agents") or ():
        agent_cfg = agents_config.get(str(agent))
        if isinstance(agent_cfg, dict) and bool(agent_cfg.get("enabled", False)):
            return True
    return False


def _collect_blockers(*, roles: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    blocking_reasons = {
        "failed_task_present",
        "human_gate_present",
        "claimed_task_stale",
        "ready_task_unconsumed",
        "no_recent_heartbeat",
        "runner_disabled",
        "scheduler_disabled",
        "reviewer_pending",
        "git_steward_pending",
    }
    for role in roles:
        reason = role.get("primary_reason")
        if reason in blocking_reasons and role.get("status") not in {"queue_empty", "healthy", "idle"}:
            blockers.append(
                {
                    "role": role.get("role"),
                    "status": role.get("status"),
                    "reason": reason,
                    "count": _blocker_count(role, str(reason)),
                    "next_action_role": role.get("next_action_role"),
                }
            )
    human_gate_count = len(
        [
            task
            for task in tasks
            if str(task.get("status")) in HUMAN_GATE_STATUSES or bool(task.get("requires_human"))
        ]
    )
    if human_gate_count and not any(blocker.get("reason") == "human_gate_present" for blocker in blockers):
        blockers.append(
            {
                "role": "human",
                "status": "waiting_human",
                "reason": "human_gate_present",
                "count": human_gate_count,
                "next_action_role": "human",
            }
        )
    return blockers


def _blocker_count(role: dict[str, Any], reason: str) -> int:
    if reason == "failed_task_present":
        return int(role.get("failed_task_count") or 0)
    if reason == "human_gate_present":
        return int(role.get("human_gate_count") or 0)
    if reason == "claimed_task_stale":
        return int(role.get("claimed_stale_count") or 0)
    if reason == "ready_task_unconsumed":
        return int(role.get("ready_task_count") or 0)
    if reason == "reviewer_pending":
        return int(role.get("review_pending_count") or 0)
    if reason == "git_steward_pending":
        return int(role.get("git_gate_pending_count") or 0)
    return 1


def _blocking_classification(blockers: list[dict[str, Any]]) -> str:
    if not blockers:
        return "clear"
    reasons = {str(blocker.get("reason")) for blocker in blockers}
    if reasons == {"human_gate_present"}:
        return "business_human_gate"
    if reasons.issubset({"reviewer_pending", "git_steward_pending"}):
        return "waiting_peer"
    return "mechanism_or_role_blocked"


def _queue_observations(
    *,
    tasks: list[dict[str, Any]],
    generated_at: str,
    thresholds: dict[str, int],
) -> dict[str, Any]:
    ready = [task for task in tasks if str(task.get("status")) in READY_STATUSES]
    stale_ready = [
        task for task in ready if _task_age_seconds(task, generated_at=generated_at) >= thresholds["ready_task_stale_seconds"]
    ]
    claimed = [task for task in tasks if str(task.get("status")) in ACTIVE_STATUSES]
    claimed_stale = [
        task
        for task in claimed
        if _claimed_age_seconds(task, generated_at=generated_at) >= thresholds["claimed_task_stale_seconds"]
    ]
    failed = [task for task in tasks if str(task.get("status")) in FAILED_STATUSES]
    human = [task for task in tasks if str(task.get("status")) in HUMAN_GATE_STATUSES or bool(task.get("requires_human"))]
    return {
        "total_task_count": len(tasks),
        "ready_task_count": len(ready),
        "stale_ready_task_count": len(stale_ready),
        "claimed_task_count": len(claimed),
        "claimed_stale_count": len(claimed_stale),
        "failed_task_count": len(failed),
        "human_gate_count": len(human),
    }


def _review_observations(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"pending_review_count": len([task for task in tasks if str(task.get("status")) in REVIEW_PENDING_STATUSES])}


def _dashboard_cards(roles: list[dict[str, Any]]) -> dict[str, Any]:
    cards = [
        {
            "role": role.get("role"),
            "display_name": role.get("display_name"),
            "status": role.get("status"),
            "reason": role.get("primary_reason"),
            "last_seen_at": role.get("last_seen_at"),
            "current_task_id": role.get("current_task_id"),
            "ready_task_count": role.get("ready_task_count", 0),
            "claimed_stale_count": role.get("claimed_stale_count", 0),
            "failed_task_count": role.get("failed_task_count", 0),
            "next_action_role": role.get("next_action_role"),
        }
        for role in roles
    ]
    return {"cards": cards, "auto_repair_actions": []}


def _next_action_role(*, role: str, reason: str | None) -> str:
    if reason == "human_gate_present":
        return "human"
    if reason == "reviewer_pending":
        return "reviewer"
    if reason == "git_steward_pending":
        return "git_steward"
    return role


def _thresholds(config: dict[str, Any]) -> dict[str, int]:
    role_health = config.get("role_health") if isinstance(config.get("role_health"), dict) else {}
    return {
        "heartbeat_stale_seconds": _minutes_to_seconds(role_health.get("heartbeat_stale_minutes"), default_minutes=30),
        "ready_task_stale_seconds": _minutes_to_seconds(role_health.get("ready_task_stale_minutes"), default_minutes=60),
        "claimed_task_stale_seconds": _minutes_to_seconds(role_health.get("claimed_task_stale_minutes"), default_minutes=30),
    }


def _minutes_to_seconds(value: Any, *, default_minutes: int) -> int:
    try:
        minutes = int(value if value is not None else default_minutes)
    except (TypeError, ValueError):
        minutes = default_minutes
    return max(1, minutes) * 60


def _task_age_seconds(task: dict[str, Any], *, generated_at: str) -> int:
    return _age_seconds(str(task.get("updated_at") or task.get("created_at") or ""), generated_at=generated_at) or 0


def _claimed_age_seconds(task: dict[str, Any], *, generated_at: str) -> int:
    return _age_seconds(str(task.get("claimed_at") or task.get("updated_at") or task.get("created_at") or ""), generated_at=generated_at) or 0


def _heartbeat_age_seconds(agent_state: dict[str, Any] | None, *, generated_at: str) -> int | None:
    if not agent_state:
        return None
    last_seen = agent_state.get("last_seen_at") or agent_state.get("updated_at")
    if not last_seen:
        return None
    return _age_seconds(str(last_seen), generated_at=generated_at)


def _age_seconds(checked_at: str, *, generated_at: str) -> int | None:
    checked = _parse_iso_datetime(checked_at)
    generated = _parse_iso_datetime(generated_at)
    if checked is None or generated is None:
        return None
    return max(0, int((generated - checked).total_seconds()))


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}

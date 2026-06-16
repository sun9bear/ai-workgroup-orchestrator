from __future__ import annotations

from pathlib import Path
from typing import Any

from aiwg.config import load_config
from aiwg.dashboard.status import READ_ONLY_CAPABILITIES, get_status_snapshot
from aiwg.state.database import resolve_project_root

READ_ONLY_TOOL_NAMES = ("status", "list_tasks", "get_task", "recent_events")


def _load_context(config_path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(config_path)
    config = load_config(path)
    project_root = resolve_project_root(config, config_path=path)
    return config, project_root


def _capabilities() -> dict[str, Any]:
    return {
        "read_only": True,
        "mutation_actions": [],
    }


def _snapshot(
    *,
    config_path: str | Path,
    recent_events: int = 10,
    task_limit: int = 50,
    status_filter: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    config, project_root = _load_context(config_path)
    return get_status_snapshot(
        config=config,
        project_root=project_root,
        recent_events=max(1, int(recent_events)),
        task_limit=max(1, int(task_limit)),
        status=status_filter,
        agent=agent,
    )


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "to_agent": task.get("to_agent"),
        "from_agent": task.get("from_agent"),
        "requires_human": bool(task.get("requires_human")),
        "can_write": bool(task.get("can_write")),
        "latest_report_path": task.get("latest_report_path"),
    }


def status_tool(
    *,
    config_path: str | Path = "aiwg.yaml",
    recent_events: int = 10,
    task_limit: int = 50,
    status_filter: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Return a read-only status snapshot for MCP clients.

    This function intentionally delegates to the existing Phase A4 read-only
    dashboard code, which opens SQLite with URI mode=ro and never initializes
    a missing database.
    """

    payload = _snapshot(
        config_path=config_path,
        recent_events=recent_events,
        task_limit=task_limit,
        status_filter=status_filter,
        agent=agent,
    )
    payload["tool"] = "status"
    payload["capabilities"] = dict(READ_ONLY_CAPABILITIES)
    payload["capabilities"]["mutation_actions"] = []
    payload["capabilities"]["read_only"] = True
    return payload


def list_tasks_tool(
    *,
    config_path: str | Path = "aiwg.yaml",
    status_filter: str | None = None,
    agent: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return read-only task summaries from SQLite status snapshot data."""

    snapshot = _snapshot(
        config_path=config_path,
        recent_events=1,
        task_limit=limit,
        status_filter=status_filter,
        agent=agent,
    )
    tasks = [_task_summary(task) for task in snapshot.get("tasks", [])]
    return {
        "tool": "list_tasks",
        "capabilities": _capabilities(),
        "database": snapshot.get("database"),
        "count": len(tasks),
        "tasks": tasks,
    }


def get_task_tool(
    *,
    config_path: str | Path = "aiwg.yaml",
    task_id: str,
) -> dict[str, Any]:
    """Return one read-only task summary by SQLite task/message id."""

    snapshot = _snapshot(config_path=config_path, recent_events=1, task_limit=1000)
    for task in snapshot.get("tasks", []):
        if task.get("id") == task_id:
            return {
                "tool": "get_task",
                "capabilities": _capabilities(),
                "found": True,
                "task": _task_summary(task),
                "task_id": task_id,
            }
    return {
        "tool": "get_task",
        "capabilities": _capabilities(),
        "found": False,
        "task": None,
        "task_id": task_id,
    }


def recent_events_tool(
    *,
    config_path: str | Path = "aiwg.yaml",
    limit: int = 10,
) -> dict[str, Any]:
    """Return newest read-only event rows from SQLite status snapshot data."""

    snapshot = _snapshot(config_path=config_path, recent_events=limit, task_limit=1)
    events = list(snapshot.get("recent_events", []))
    return {
        "tool": "recent_events",
        "capabilities": _capabilities(),
        "database": snapshot.get("database"),
        "count": len(events),
        "events": events,
    }

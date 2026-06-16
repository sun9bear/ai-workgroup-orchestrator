from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aiwg.state.database import resolve_db_path, utc_now_iso

EXTERNAL_REVIEW_GATE_SCHEMA_VERSION = "aiwg.external_review_gate.v1"
EXTERNAL_REVIEW_GATE_STATUSES = (
    "not_polled",
    "no_pr",
    "pending_review",
    "approved",
    "changes_requested",
    "blocked",
    "ci_failed",
    "stale",
    "unknown",
)
EXTERNAL_REVIEW_SOURCE_TYPES = (
    "github_pr",
    "codex_report",
    "reviewer_report",
    "human_report",
    "ci",
    "coderabbit",
    "security_scanner",
    "other",
)
FEEDBACK_CATEGORIES = (
    "must_fix",
    "should_fix",
    "question",
    "non_blocking",
    "human_gate",
    "out_of_scope",
)
ACTIONABLE_FEEDBACK_CATEGORIES = {"must_fix", "should_fix", "question", "human_gate"}
ACTIONABLE_FEEDBACK_PRIORITY = {
    "human_gate": 0,
    "must_fix": 1,
    "should_fix": 2,
    "question": 3,
}
BLOCKING_FEEDBACK_CATEGORIES = {"must_fix", "human_gate"}
RESOLVED_ITEM_STATES = {"resolved", "dismissed"}


def get_external_review_gate_snapshot(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a D4.3 read-only external review gate snapshot.

    D4.3 deliberately treats review gates as observer state, not actions. This
    function reads existing SQLite rows with URI ``mode=ro`` and never polls
    GitHub, comments on PRs, creates fix tasks, pushes, merges, or mutates a
    protected target/business repository.
    """

    project_root_path = Path(project_root)
    db_path = resolve_db_path(config, project_root_path)
    generated_at = generated_at or utc_now_iso()
    stale_after_seconds = _stale_after_seconds(config)
    snapshot = _empty_snapshot(
        db_path=db_path,
        generated_at=generated_at,
        stale_after_seconds=stale_after_seconds,
    )
    if not db_path.exists():
        snapshot["gate_state"] = "not_polled"
        snapshot["classification"] = classify_external_review_items(
            sources=[],
            items=[],
            generated_at=generated_at,
            stale_after_seconds=stale_after_seconds,
            missing_database=True,
        )
        return snapshot

    with _connect_readonly(db_path) as conn:
        table_names = _table_names(conn)
        sources = _read_sources(conn) if "external_review_sources" in table_names else []
        items = _read_items(conn) if "external_review_items" in table_names else []
        persisted_snapshots = (
            _read_persisted_snapshots(conn) if "external_review_gate_snapshots" in table_names else []
        )

    classification = classify_external_review_items(
        sources=sources,
        items=items,
        generated_at=generated_at,
        stale_after_seconds=stale_after_seconds,
    )
    normalized_sources = classification.get("normalized_sources") or sources
    normalized_items = classification.get("normalized_items") or items
    snapshot.update(
        {
            "gate_state": classification["gate_state"],
            "sources": normalized_sources,
            "items": normalized_items,
            "actionable_feedback": classification["actionable_feedback"],
            "safety_warnings": classification["safety_warnings"],
            "classification": classification,
            "sources_summary": _sources_summary(normalized_sources),
            "items_summary": _items_summary(normalized_items, classification=classification),
            "persisted_snapshots": persisted_snapshots,
        }
    )
    return snapshot


def classify_external_review_items(
    *,
    sources: Iterable[dict[str, Any]],
    items: Iterable[dict[str, Any]],
    generated_at: str | None = None,
    stale_after_seconds: int | None = None,
    missing_database: bool = False,
) -> dict[str, Any]:
    generated_at = generated_at or utc_now_iso()
    stale_after_seconds = 120 * 60 if stale_after_seconds is None else int(stale_after_seconds)
    source_rows = [_normalize_source(source, generated_at=generated_at, stale_after_seconds=stale_after_seconds) for source in sources]
    item_rows = [_normalize_item(item) for item in items]

    feedback_counts = {category: 0 for category in FEEDBACK_CATEGORIES}
    for item in item_rows:
        category = str(item.get("feedback_category") or "unknown")
        if category in feedback_counts:
            feedback_counts[category] += 1

    unresolved_items = [item for item in item_rows if not _item_resolved(item)]
    actionable_feedback = sorted(
        (
            item
            for item in unresolved_items
            if str(item.get("feedback_category")) in ACTIONABLE_FEEDBACK_CATEGORIES
        ),
        key=lambda item: (
            ACTIONABLE_FEEDBACK_PRIORITY.get(str(item.get("feedback_category")), 99),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
    )
    blocking_feedback = [
        item
        for item in unresolved_items
        if str(item.get("feedback_category")) in BLOCKING_FEEDBACK_CATEGORIES or bool(item.get("blocking"))
    ]
    safety_warnings = [warning for source in source_rows for warning in source.get("safety_warnings", [])]
    human_gate_feedback = [
        item for item in unresolved_items if str(item.get("feedback_category")) == "human_gate"
    ]
    must_fix_feedback = [
        item
        for item in unresolved_items
        if str(item.get("feedback_category")) == "must_fix" or bool(item.get("blocking"))
    ]
    question_feedback = [
        item for item in unresolved_items if str(item.get("feedback_category")) == "question"
    ]

    source_state_counts: dict[str, int] = {state: 0 for state in EXTERNAL_REVIEW_GATE_STATUSES}
    source_type_counts: dict[str, int] = {source_type: 0 for source_type in EXTERNAL_REVIEW_SOURCE_TYPES}
    for source in source_rows:
        state = str(source.get("effective_gate_state") or source.get("gate_state") or "unknown")
        source_state_counts[state if state in source_state_counts else "unknown"] += 1
        source_type = str(source.get("source_type") or "other")
        source_type_counts[source_type if source_type in source_type_counts else "other"] += 1

    gate_state = _classify_gate_state(
        sources=source_rows,
        items=item_rows,
        missing_database=missing_database,
        safety_warning_count=len(safety_warnings),
        human_gate_count=len(human_gate_feedback),
        must_fix_count=len(must_fix_feedback),
        question_count=len(question_feedback),
    )
    return {
        "gate_state": gate_state,
        "feedback_counts": feedback_counts,
        "source_state_counts": source_state_counts,
        "source_type_counts": source_type_counts,
        "source_count": len(source_rows),
        "item_count": len(item_rows),
        "unresolved_item_count": len(unresolved_items),
        "unresolved_actionable_count": len(actionable_feedback),
        "blocking_feedback_count": len(blocking_feedback),
        "safety_warning_count": len(safety_warnings),
        "safety_warnings": safety_warnings,
        "human_gate_count": len(human_gate_feedback),
        "must_fix_count": len(must_fix_feedback),
        "question_count": len(question_feedback),
        "actionable_feedback": actionable_feedback,
        "normalized_sources": source_rows,
        "normalized_items": item_rows,
        "read_only": True,
        "mutation_actions": [],
    }


def render_external_review_gate_text(snapshot: dict[str, Any]) -> str:
    sources_summary = snapshot.get("sources_summary") or {}
    items_summary = snapshot.get("items_summary") or {}
    lines = [
        "External review gate",
        f"generated_at: {snapshot.get('generated_at')}",
        f"database: {(snapshot.get('database') or {}).get('path')}",
        "capabilities: read_only=true; mutation_actions=[]",
        f"status={snapshot.get('gate_state')}",
        f"sources={sources_summary.get('source_count', 0)} items={items_summary.get('item_count', 0)} "
        f"unresolved_actionable={items_summary.get('unresolved_actionable_count', 0)} "
        f"blocking_feedback={items_summary.get('blocking_feedback_count', 0)}",
        "",
        "Sources",
    ]
    sources = snapshot.get("sources") or []
    if sources:
        for source in sources:
            lines.append(
                f"- {source.get('id')} | type={source.get('source_type')} | "
                f"state={source.get('effective_gate_state') or source.get('gate_state')} | "
                f"ref={source.get('provider_ref') or '-'}"
            )
    else:
        lines.append("- none")

    safety_warnings = snapshot.get("safety_warnings") or []
    if safety_warnings:
        lines.extend(["", "Safety warnings"])
        for warning in safety_warnings:
            lines.append(
                f"- {warning.get('code')} | source={warning.get('source_id') or '-'} | "
                f"mutation_actions={warning.get('mutation_actions') or []}"
            )

    lines.extend(["", "Actionable feedback"])
    actionable = snapshot.get("actionable_feedback") or []
    if actionable:
        for item in actionable:
            location = item.get("file_path") or "-"
            if item.get("line") is not None:
                location = f"{location}:{item.get('line')}"
            lines.append(
                f"- {item.get('id')} | category={item.get('feedback_category')} | "
                f"blocking={str(bool(item.get('blocking'))).lower()} | {location} | {item.get('title')}"
            )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _empty_snapshot(db_path: Path, generated_at: str, stale_after_seconds: int) -> dict[str, Any]:
    return {
        "schema_version": EXTERNAL_REVIEW_GATE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "database": {
            "path": str(db_path),
            "exists": db_path.exists(),
            "mode": "read_only",
        },
        "read_only": True,
        "mutation_actions": [],
        "gate_statuses": list(EXTERNAL_REVIEW_GATE_STATUSES),
        "source_types": list(EXTERNAL_REVIEW_SOURCE_TYPES),
        "feedback_categories": list(FEEDBACK_CATEGORIES),
        "thresholds": {"stale_after_seconds": stale_after_seconds},
        "gate_state": "no_pr" if db_path.exists() else "not_polled",
        "sources": [],
        "items": [],
        "actionable_feedback": [],
        "classification": {},
        "sources_summary": {"source_count": 0, "source_type_counts": {}, "source_state_counts": {}},
        "items_summary": {
            "item_count": 0,
            "unresolved_actionable_count": 0,
            "blocking_feedback_count": 0,
            "feedback_counts": {category: 0 for category in FEEDBACK_CATEGORIES},
        },
        "safety_warnings": [],
        "persisted_snapshots": [],
        "github_write_api_called": False,
        "github_write_api_call_count": 0,
        "git_push_performed": False,
        "git_merge_performed": False,
        "pr_comment_performed": False,
        "created_fix_tasks": False,
        "target_writes_performed": False,
        "ready_for_real_agent_execution": False,
        "ready_for_protected_business_repository_write": False,
        "mcp_mutation_tools_exposed": False,
        "codex_automation_modified": False,
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


def _read_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, source_type, display_name, provider_ref, gate_state, last_polled_at,
               read_only, mutation_actions_json, payload_json, created_at, updated_at
        FROM external_review_sources
        ORDER BY id
        """
    ).fetchall()
    sources: list[dict[str, Any]] = []
    for row in rows:
        source = dict(row)
        source["read_only"] = bool(source.get("read_only"))
        raw_mutation_actions_json = source.pop("mutation_actions_json", None)
        source["mutation_actions_json_raw"] = raw_mutation_actions_json
        source["mutation_actions"] = _parse_json_array(raw_mutation_actions_json)
        source["payload"] = _parse_json_object(source.pop("payload_json", None))
        sources.append(source)
    return sources


def _read_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, source_id, source_type, item_state, feedback_category, title, body,
               file_path, line, resolved, blocking, payload_json, created_at, updated_at
        FROM external_review_items
        ORDER BY created_at, id
        """
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["resolved"] = bool(item.get("resolved"))
        item["blocking"] = bool(item.get("blocking"))
        item["payload"] = _parse_json_object(item.pop("payload_json", None))
        items.append(item)
    return items


def _read_persisted_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, gate_state, source_count, item_count, unresolved_actionable_count,
               summary_json, read_only, mutation_actions_json, git_push_performed,
               git_merge_performed, pr_comment_performed, target_writes_performed, created_at
        FROM external_review_gate_snapshots
        ORDER BY created_at DESC, id DESC
        LIMIT 5
        """
    ).fetchall()
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        snapshot = dict(row)
        snapshot["read_only"] = bool(snapshot.get("read_only"))
        snapshot["git_push_performed"] = bool(snapshot.get("git_push_performed"))
        snapshot["git_merge_performed"] = bool(snapshot.get("git_merge_performed"))
        snapshot["pr_comment_performed"] = bool(snapshot.get("pr_comment_performed"))
        snapshot["target_writes_performed"] = bool(snapshot.get("target_writes_performed"))
        snapshot["mutation_actions"] = _parse_json_array(snapshot.pop("mutation_actions_json", None))
        snapshot["summary"] = _parse_json_object(snapshot.pop("summary_json", None))
        snapshots.append(snapshot)
    return snapshots


def _normalize_source(source: dict[str, Any], *, generated_at: str, stale_after_seconds: int) -> dict[str, Any]:
    normalized = dict(source)
    state = str(normalized.get("gate_state") or "unknown")
    if state not in EXTERNAL_REVIEW_GATE_STATUSES:
        state = "unknown"
    normalized["gate_state"] = state
    normalized["read_only"] = bool(normalized.get("read_only", True))
    raw_mutation_actions = normalized.get("mutation_actions")
    raw_mutation_actions_json = normalized.get("mutation_actions_json_raw")
    mutation_actions = raw_mutation_actions if isinstance(raw_mutation_actions, list) else []
    normalized["mutation_actions"] = mutation_actions
    mutation_actions_json_dirty = _mutation_actions_json_has_non_empty_value(raw_mutation_actions_json)
    non_list_mutation_actions_dirty = (
        raw_mutation_actions is not None and raw_mutation_actions != "" and not isinstance(raw_mutation_actions, list)
    )
    safety_warnings = []
    if not normalized["read_only"]:
        safety_warnings.append(
            {
                "code": "external_review_source_not_read_only",
                "source_id": normalized.get("id"),
                "source_type": normalized.get("source_type") or "other",
                "message": "External review source row is not read-only; treating gate as blocked.",
            }
        )
    if mutation_actions or mutation_actions_json_dirty or non_list_mutation_actions_dirty:
        warning = {
            "code": "external_review_source_mutation_actions_present",
            "source_id": normalized.get("id"),
            "source_type": normalized.get("source_type") or "other",
            "mutation_actions": list(mutation_actions),
            "message": "External review source row exposes mutation actions; treating gate as blocked.",
        }
        if raw_mutation_actions_json is not None:
            warning["raw_mutation_actions_json"] = str(raw_mutation_actions_json)
        elif non_list_mutation_actions_dirty:
            warning["raw_mutation_actions"] = str(raw_mutation_actions)
        safety_warnings.append(warning)
    normalized["safety_warnings"] = safety_warnings
    stale = _is_source_stale(normalized, generated_at=generated_at, stale_after_seconds=stale_after_seconds)
    normalized["stale"] = stale
    if safety_warnings:
        normalized["effective_gate_state"] = "blocked"
    else:
        normalized["effective_gate_state"] = "stale" if stale and state not in {"approved", "no_pr"} else state
    return normalized


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    category = str(normalized.get("feedback_category") or "out_of_scope")
    if category not in FEEDBACK_CATEGORIES:
        category = "out_of_scope"
    normalized["feedback_category"] = category
    item_state = str(normalized.get("item_state") or "open")
    normalized["item_state"] = item_state if item_state in {"open", "resolved", "dismissed", "stale"} else "open"
    normalized["resolved"] = bool(normalized.get("resolved")) or normalized["item_state"] in RESOLVED_ITEM_STATES
    normalized["blocking"] = bool(normalized.get("blocking"))
    return normalized


def _classify_gate_state(
    *,
    sources: list[dict[str, Any]],
    items: list[dict[str, Any]],
    missing_database: bool,
    safety_warning_count: int,
    human_gate_count: int,
    must_fix_count: int,
    question_count: int,
) -> str:
    if missing_database:
        return "not_polled"
    if not sources and not items:
        return "no_pr"
    if safety_warning_count:
        return "blocked"
    if sources and all(not source.get("last_polled_at") for source in sources) and not items:
        return "not_polled"

    states = {str(source.get("effective_gate_state") or source.get("gate_state") or "unknown") for source in sources}
    if human_gate_count or "blocked" in states:
        return "blocked"
    if "ci_failed" in states:
        return "ci_failed"
    if must_fix_count or "changes_requested" in states:
        return "changes_requested"
    if "pending_review" in states or question_count:
        return "pending_review"
    if "stale" in states:
        return "stale"
    if sources and states <= {"approved", "no_pr"}:
        return "approved"
    if "not_polled" in states:
        return "not_polled"
    return "unknown"


def _sources_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    source_type_counts = {source_type: 0 for source_type in EXTERNAL_REVIEW_SOURCE_TYPES}
    source_state_counts = {state: 0 for state in EXTERNAL_REVIEW_GATE_STATUSES}
    for source in sources:
        source_type = str(source.get("source_type") or "other")
        source_type_counts[source_type if source_type in source_type_counts else "other"] += 1
        state = str(source.get("effective_gate_state") or source.get("gate_state") or "unknown")
        source_state_counts[state if state in source_state_counts else "unknown"] += 1
    return {
        "source_count": len(sources),
        "source_type_counts": source_type_counts,
        "source_state_counts": source_state_counts,
    }


def _items_summary(items: list[dict[str, Any]], *, classification: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_count": len(items),
        "feedback_counts": classification.get("feedback_counts") or {category: 0 for category in FEEDBACK_CATEGORIES},
        "unresolved_item_count": classification.get("unresolved_item_count", 0),
        "unresolved_actionable_count": classification.get("unresolved_actionable_count", 0),
        "blocking_feedback_count": classification.get("blocking_feedback_count", 0),
        "human_gate_count": classification.get("human_gate_count", 0),
        "must_fix_count": classification.get("must_fix_count", 0),
        "question_count": classification.get("question_count", 0),
    }


def _stale_after_seconds(config: dict[str, Any]) -> int:
    gate_config = config.get("external_review_gate") if isinstance(config, dict) else None
    if not isinstance(gate_config, dict):
        return 120 * 60
    try:
        return max(60, int(gate_config.get("stale_after_minutes", 120)) * 60)
    except (TypeError, ValueError):
        return 120 * 60


def _is_source_stale(source: dict[str, Any], *, generated_at: str, stale_after_seconds: int) -> bool:
    if str(source.get("gate_state")) == "stale":
        return True
    last_polled_at = source.get("last_polled_at")
    if not last_polled_at:
        return False
    try:
        generated = _parse_time(generated_at)
        last_polled = _parse_time(str(last_polled_at))
    except ValueError:
        return False
    return (generated - last_polled).total_seconds() >= stale_after_seconds


def _item_resolved(item: dict[str, Any]) -> bool:
    return bool(item.get("resolved")) or str(item.get("item_state")) in RESOLVED_ITEM_STATES


def _parse_time(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mutation_actions_json_has_non_empty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return bool(value)
    text = str(value).strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return True
    if isinstance(parsed, list):
        return bool(parsed)
    return True


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

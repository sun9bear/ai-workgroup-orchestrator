from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.external_review_gate import (
    classify_external_review_items,
    get_external_review_gate_snapshot,
    render_external_review_gate_text,
)
from aiwg.state.database import connect_database, init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def iso_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_d43_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["external_review_gate"] = {
        "stale_after_minutes": 120,
        "github_read_adapter_enabled": False,
        "write_back_enabled": False,
        "create_fix_tasks_enabled": False,
    }
    return config


def write_config(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config), encoding="utf-8")
    return path


def db_digest(db_path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(db_path) as conn:
        optional_counts = []
        for table in ("external_review_sources", "external_review_items", "external_review_gate_snapshots"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                optional_counts.append((table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
        return (
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            tuple(optional_counts),
            conn.execute("SELECT id, gate_state, read_only FROM external_review_sources ORDER BY id").fetchall()
            if optional_counts
            else (),
            conn.execute("SELECT id, feedback_category, resolved, blocking FROM external_review_items ORDER BY id").fetchall()
            if optional_counts
            else (),
        )


def seed_external_review_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_type: str,
    gate_state: str,
    display_name: str | None = None,
    provider_ref: str | None = None,
    last_polled_minutes_ago: int | None = 5,
    read_only: bool = True,
    mutation_actions: list[str] | None = None,
    mutation_actions_json: str | None = None,
) -> None:
    now = iso_minutes_ago(0)
    conn.execute(
        """
        INSERT INTO external_review_sources(
          id, source_type, display_name, provider_ref, gate_state, last_polled_at,
          read_only, mutation_actions_json, payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
        """,
        (
            source_id,
            source_type,
            display_name or source_id,
            provider_ref,
            gate_state,
            iso_minutes_ago(last_polled_minutes_ago) if last_polled_minutes_ago is not None else None,
            1 if read_only else 0,
            mutation_actions_json if mutation_actions_json is not None else json.dumps(mutation_actions or []),
            now,
            now,
        ),
    )


def seed_external_review_item(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    source_id: str,
    source_type: str,
    feedback_category: str,
    title: str,
    resolved: bool = False,
    blocking: bool = False,
    item_state: str = "open",
    file_path: str | None = None,
    line: int | None = None,
) -> None:
    now = iso_minutes_ago(0)
    conn.execute(
        """
        INSERT INTO external_review_items(
          id, source_id, source_type, item_state, feedback_category, title, body,
          file_path, line, resolved, blocking, payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, '{}', ?, ?)
        """,
        (
            item_id,
            source_id,
            source_type,
            item_state,
            feedback_category,
            title,
            file_path,
            line,
            1 if resolved else 0,
            1 if blocking else 0,
            now,
            now,
        ),
    )


def test_d43_external_review_gate_schema_migration_and_enums(tmp_path: Path) -> None:
    config = build_d43_config(tmp_path)

    db_path = init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
        ]
        table_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        }
        assert {
            "external_review_sources",
            "external_review_items",
            "external_review_gate_snapshots",
        }.issubset(table_names)
        source_columns = {row[1] for row in conn.execute("PRAGMA table_info(external_review_sources)")}
        assert {
            "id",
            "source_type",
            "display_name",
            "provider_ref",
            "gate_state",
            "last_polled_at",
            "read_only",
            "mutation_actions_json",
            "payload_json",
            "created_at",
            "updated_at",
        }.issubset(source_columns)
        item_columns = {row[1] for row in conn.execute("PRAGMA table_info(external_review_items)")}
        assert {
            "id",
            "source_id",
            "source_type",
            "item_state",
            "feedback_category",
            "title",
            "body",
            "file_path",
            "line",
            "resolved",
            "blocking",
            "payload_json",
            "created_at",
            "updated_at",
        }.issubset(item_columns)
        snapshot_columns = {row[1] for row in conn.execute("PRAGMA table_info(external_review_gate_snapshots)")}
        assert {
            "id",
            "gate_state",
            "source_count",
            "item_count",
            "unresolved_actionable_count",
            "summary_json",
            "read_only",
            "mutation_actions_json",
            "git_push_performed",
            "git_merge_performed",
            "pr_comment_performed",
            "target_writes_performed",
            "created_at",
        }.issubset(snapshot_columns)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        with pytest.raises(sqlite3.IntegrityError):
            seed_external_review_source(
                conn,
                source_id="bad-source-type",
                source_type="github_only_hardcoded",
                gate_state="pending_review",
            )
        with pytest.raises(sqlite3.IntegrityError):
            seed_external_review_source(
                conn,
                source_id="bad-gate-state",
                source_type="github_pr",
                gate_state="auto_merge_ready",
            )
        seed_external_review_source(
            conn,
            source_id="good-source",
            source_type="codex_report",
            gate_state="approved",
        )
        with pytest.raises(sqlite3.IntegrityError):
            seed_external_review_item(
                conn,
                item_id="bad-category",
                source_id="good-source",
                source_type="codex_report",
                feedback_category="secret_fix_it_now",
                title="Invalid category should be rejected",
            )


def test_classify_external_review_items_prioritizes_states_and_feedback_categories() -> None:
    base_sources = [
        {"id": "github", "source_type": "github_pr", "gate_state": "approved", "last_polled_at": iso_minutes_ago(5)},
        {"id": "codex", "source_type": "codex_report", "gate_state": "approved", "last_polled_at": iso_minutes_ago(5)},
        {"id": "reviewer", "source_type": "reviewer_report", "gate_state": "approved", "last_polled_at": iso_minutes_ago(5)},
    ]
    items = [
        {
            "id": "must",
            "source_id": "github",
            "source_type": "github_pr",
            "feedback_category": "must_fix",
            "resolved": False,
            "blocking": True,
            "title": "Fix the authorization bypass",
        },
        {
            "id": "should",
            "source_id": "codex",
            "source_type": "codex_report",
            "feedback_category": "should_fix",
            "resolved": False,
            "blocking": False,
            "title": "Tighten naming",
        },
        {
            "id": "question",
            "source_id": "reviewer",
            "source_type": "reviewer_report",
            "feedback_category": "question",
            "resolved": False,
            "blocking": False,
            "title": "Clarify acceptance evidence",
        },
        {
            "id": "non-blocking",
            "source_id": "codex",
            "source_type": "codex_report",
            "feedback_category": "non_blocking",
            "resolved": False,
            "blocking": False,
            "title": "Nit",
        },
        {
            "id": "out-of-scope",
            "source_id": "reviewer",
            "source_type": "reviewer_report",
            "feedback_category": "out_of_scope",
            "resolved": False,
            "blocking": False,
            "title": "Future phase",
        },
    ]

    classification = classify_external_review_items(sources=base_sources, items=items)

    assert classification["gate_state"] == "changes_requested"
    assert classification["feedback_counts"] == {
        "must_fix": 1,
        "should_fix": 1,
        "question": 1,
        "non_blocking": 1,
        "human_gate": 0,
        "out_of_scope": 1,
    }
    assert classification["unresolved_actionable_count"] == 3
    assert classification["blocking_feedback_count"] == 1
    assert [item["id"] for item in classification["actionable_feedback"]] == ["must", "should", "question"]

    assert classify_external_review_items(
        sources=base_sources,
        items=[
            {
                "id": "human",
                "source_id": "human",
                "source_type": "human_report",
                "feedback_category": "human_gate",
                "resolved": False,
                "blocking": True,
                "title": "Human approval required",
            }
        ],
    )["gate_state"] == "blocked"
    assert classify_external_review_items(
        sources=[{"id": "ci", "source_type": "ci", "gate_state": "ci_failed", "last_polled_at": iso_minutes_ago(2)}],
        items=[],
    )["gate_state"] == "ci_failed"
    assert classify_external_review_items(sources=base_sources, items=[])["gate_state"] == "approved"
    assert classify_external_review_items(sources=[], items=[])["gate_state"] == "no_pr"


def test_external_review_gate_snapshot_is_generic_read_only_and_non_mutating(tmp_path: Path) -> None:
    config = build_d43_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_external_review_source(
            conn,
            source_id="gh-pr-42",
            source_type="github_pr",
            gate_state="pending_review",
            display_name="GitHub PR #42",
            provider_ref="https://example.invalid/org/repo/pull/42",
        )
        seed_external_review_source(
            conn,
            source_id="codex-review-d43",
            source_type="codex_report",
            gate_state="approved",
            display_name="CodeX D4.3 report",
        )
        seed_external_review_source(
            conn,
            source_id="reviewer-report-d43",
            source_type="reviewer_report",
            gate_state="changes_requested",
            display_name="Reviewer agent report",
        )
        seed_external_review_item(
            conn,
            item_id="reviewer-must-fix",
            source_id="reviewer-report-d43",
            source_type="reviewer_report",
            feedback_category="must_fix",
            title="Preserve read-only boundary",
            blocking=True,
            file_path="aiwg/external_review_gate.py",
            line=120,
        )
        seed_external_review_item(
            conn,
            item_id="codex-question",
            source_id="codex-review-d43",
            source_type="codex_report",
            feedback_category="question",
            title="Confirm no PR comments are written",
        )
        seed_external_review_item(
            conn,
            item_id="github-nit-resolved",
            source_id="gh-pr-42",
            source_type="github_pr",
            feedback_category="non_blocking",
            title="Resolved nit",
            resolved=True,
        )
    before = db_digest(db_path)

    snapshot = get_external_review_gate_snapshot(config=config, project_root=tmp_path)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["schema_version"] == "aiwg.external_review_gate.v1"
    assert snapshot["gate_state"] == "changes_requested"
    assert snapshot["read_only"] is True
    assert snapshot["mutation_actions"] == []
    assert snapshot["sources_summary"]["source_count"] == 3
    assert snapshot["items_summary"]["item_count"] == 3
    assert snapshot["classification"]["unresolved_actionable_count"] == 2
    assert snapshot["classification"]["blocking_feedback_count"] == 1
    assert {source["source_type"] for source in snapshot["sources"]} == {
        "github_pr",
        "codex_report",
        "reviewer_report",
    }
    assert [item["feedback_category"] for item in snapshot["actionable_feedback"]] == ["must_fix", "question"]
    assert snapshot["github_write_api_called"] is False
    assert snapshot["git_push_performed"] is False
    assert snapshot["git_merge_performed"] is False
    assert snapshot["pr_comment_performed"] is False
    assert snapshot["created_fix_tasks"] is False
    assert snapshot["ready_for_real_agent_execution"] is False
    assert snapshot["ready_for_protected_business_repository_write"] is False
    assert snapshot["mcp_mutation_tools_exposed"] is False
    rendered = render_external_review_gate_text(snapshot)
    assert "External review gate" in rendered
    assert "status=changes_requested" in rendered
    assert "mutation_actions=[]" in rendered


def test_external_review_gate_blocks_and_warns_on_write_capable_source_rows(tmp_path: Path) -> None:
    config = build_d43_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_external_review_source(
            conn,
            source_id="legacy-write-capable-source",
            source_type="github_pr",
            gate_state="approved",
            display_name="Legacy write-capable PR source",
            read_only=False,
            mutation_actions=["comment_pr"],
        )
    before = db_digest(db_path)

    snapshot = get_external_review_gate_snapshot(config=config, project_root=tmp_path)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["read_only"] is True
    assert snapshot["mutation_actions"] == []
    assert snapshot["gate_state"] == "blocked"
    assert snapshot["classification"]["safety_warning_count"] == 2
    assert [warning["code"] for warning in snapshot["safety_warnings"]] == [
        "external_review_source_not_read_only",
        "external_review_source_mutation_actions_present",
    ]
    assert snapshot["safety_warnings"][0]["source_id"] == "legacy-write-capable-source"
    assert snapshot["safety_warnings"][1]["mutation_actions"] == ["comment_pr"]
    assert snapshot["sources"][0]["effective_gate_state"] == "blocked"
    assert snapshot["sources"][0]["safety_warnings"] == snapshot["safety_warnings"]
    rendered = render_external_review_gate_text(snapshot)
    assert "Safety warnings" in rendered
    assert "external_review_source_not_read_only" in rendered
    assert "external_review_source_mutation_actions_present" in rendered


@pytest.mark.parametrize("raw_mutation_actions_json", ['"comment_pr"', '{"action":"comment_pr"}', 'comment_pr'])
def test_external_review_gate_blocks_non_array_or_malformed_mutation_actions_json(
    tmp_path: Path,
    raw_mutation_actions_json: str,
) -> None:
    config = build_d43_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_external_review_source(
            conn,
            source_id="legacy-malformed-mutation-source",
            source_type="github_pr",
            gate_state="approved",
            display_name="Legacy malformed mutation source",
            mutation_actions_json=raw_mutation_actions_json,
        )
    before = db_digest(db_path)

    snapshot = get_external_review_gate_snapshot(config=config, project_root=tmp_path)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["read_only"] is True
    assert snapshot["mutation_actions"] == []
    assert snapshot["gate_state"] == "blocked"
    assert snapshot["classification"]["safety_warning_count"] == 1
    assert snapshot["safety_warnings"] == [
        {
            "code": "external_review_source_mutation_actions_present",
            "source_id": "legacy-malformed-mutation-source",
            "source_type": "github_pr",
            "mutation_actions": [],
            "raw_mutation_actions_json": raw_mutation_actions_json,
            "message": "External review source row exposes mutation actions; treating gate as blocked.",
        }
    ]
    assert snapshot["sources"][0]["effective_gate_state"] == "blocked"


def test_external_review_gate_cli_and_status_dashboard_are_read_only(tmp_path: Path) -> None:
    config = build_d43_config(tmp_path)
    config_path = write_config(tmp_path, config)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_external_review_source(
            conn,
            source_id="human-approval-note",
            source_type="human_report",
            gate_state="approved",
            display_name="Human report",
        )
        seed_external_review_item(
            conn,
            item_id="human-non-blocking-note",
            source_id="human-approval-note",
            source_type="human_report",
            feedback_category="non_blocking",
            title="FYI only",
        )
    before = db_digest(db_path)

    cli_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "external-review-gate",
            "--config",
            str(config_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert cli_result.returncode == 0, cli_result.stderr + cli_result.stdout
    payload = json.loads(cli_result.stdout)
    assert payload["gate_state"] == "approved"
    assert payload["read_only"] is True
    assert payload["mutation_actions"] == []
    assert payload["pr_comment_performed"] is False

    text_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "external-review-gate",
            "--config",
            str(config_path),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert text_result.returncode == 0, text_result.stderr + text_result.stdout
    assert "External review gate" in text_result.stdout
    assert "status=approved" in text_result.stdout

    status_snapshot = get_status_snapshot(config=config, project_root=tmp_path)
    assert status_snapshot["external_review_gate"]["gate_state"] == "approved"
    rendered_status = render_status_text(status_snapshot)
    assert "External review gate" in rendered_status
    assert "status=approved" in rendered_status
    assert db_digest(db_path) == before


def test_external_review_gate_does_not_write_target_business_repository(tmp_path: Path) -> None:
    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "protected-business-repo"
    orchestrator_root.mkdir()
    target_root.mkdir()
    (target_root / "sentinel.txt").write_text("business repo sentinel", encoding="utf-8")
    config = build_d43_config(orchestrator_root)
    config["project_root"] = str(orchestrator_root)
    config["external_review_gate"]["target_root"] = str(target_root)
    config_path = write_config(orchestrator_root, config)
    init_database(config=config, project_root=orchestrator_root)

    snapshot = get_external_review_gate_snapshot(config=config, project_root=orchestrator_root)
    cli_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "external-review-gate",
            "--config",
            str(config_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert snapshot["target_writes_performed"] is False
    assert cli_result.returncode == 0, cli_result.stderr + cli_result.stdout
    assert json.loads(cli_result.stdout)["target_writes_performed"] is False
    assert (target_root / "sentinel.txt").read_text(encoding="utf-8") == "business repo sentinel"
    assert not list(target_root.rglob("external-review*"))
    assert not list(target_root.rglob("review-gate*"))
    assert not list(target_root.rglob("*.sqlite"))

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.role_health import get_role_health_snapshot, render_role_health_text
from aiwg.state.database import connect_database, init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def iso_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_d42_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["role_health"] = {
        "heartbeat_stale_minutes": 30,
        "ready_task_stale_minutes": 45,
        "claimed_task_stale_minutes": 20,
    }
    for agent in ("Codex", "Hermes", "Claude-Code", "OpenCode"):
        config["agents"][agent]["enabled"] = True
    config["agents"]["Reviewer"] = {"adapter": "codex_review", "enabled": True, "can_write": False}
    return config


def write_config(tmp_path: Path, config: dict[str, Any]) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config), encoding="utf-8")
    return path


def db_digest(db_path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(db_path) as conn:
        optional_counts = []
        for table in ("agent_states", "agent_health_events"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                optional_counts.append((table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
        return (
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0],
            tuple(optional_counts),
            conn.execute("SELECT id, status, claimed_by, claimed_at, updated_at FROM tasks ORDER BY id").fetchall(),
        )


def seed_task(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    to_agent: str,
    status: str,
    updated_minutes_ago: int = 0,
    claimed_minutes_ago: int | None = None,
    requires_human: bool = False,
    task_id: str | None = None,
) -> None:
    updated_at = iso_minutes_ago(updated_minutes_ago)
    claimed_at = iso_minutes_ago(claimed_minutes_ago) if claimed_minutes_ago is not None else None
    conn.execute(
        """
        INSERT INTO tasks(
          id, task_id, message_path, from_agent, to_agent, type, status, priority,
          requires_human, can_write, worktree_required, max_scope, review_delegate,
          allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
          claimed_by, claimed_at, lock_id, attempt, max_attempts, timeout_minutes,
          created_at, updated_at
        ) VALUES (?, ?, ?, 'CodeX', ?, 'instruction', ?, 'medium', ?, 0, 0, 'limited', 'CodeX',
                  '[]', '[]', '[]', '[]', ?, ?, ?, 0, 2, 30, ?, ?)
        """,
        (
            message_id,
            task_id or message_id,
            f"docs/ai-workgroup/inbox/{to_agent}/{message_id}.md",
            to_agent,
            status,
            1 if requires_human else 0,
            to_agent if status in {"claimed", "working"} else None,
            claimed_at,
            f"lock-{message_id}" if status in {"claimed", "working"} else None,
            updated_at,
            updated_at,
        ),
    )


def role_by_name(snapshot: dict[str, Any], role: str) -> dict[str, Any]:
    return {item["role"]: item for item in snapshot["roles"]}[role]


def seed_agent_state(
    conn: sqlite3.Connection,
    *,
    role: str,
    health_status: str,
    health_reason: str | None,
    last_seen_minutes_ago: int | None = 5,
    enabled: bool = True,
) -> None:
    last_seen_at = iso_minutes_ago(last_seen_minutes_ago) if last_seen_minutes_ago is not None else None
    conn.execute(
        """
        INSERT INTO agent_states(
          role, display_name, adapter_type, enabled, health_status, health_reason,
          last_seen_at, current_task_id, detail_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '{}', ?)
        """,
        (
            role,
            role.replace("_", " ").title(),
            "fixture",
            1 if enabled else 0,
            health_status,
            health_reason,
            last_seen_at,
            iso_minutes_ago(0),
        ),
    )


def test_d42_role_health_schema_migration_is_installed(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)

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
        assert {"agent_states", "agent_health_events"}.issubset(table_names)
        agent_state_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_states)")}
        assert {
            "role",
            "display_name",
            "adapter_type",
            "enabled",
            "health_status",
            "health_reason",
            "last_seen_at",
            "current_task_id",
            "detail_json",
            "updated_at",
        }.issubset(agent_state_columns)
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_health_events)")}
        assert {
            "id",
            "role",
            "event_type",
            "health_status",
            "health_reason",
            "task_id",
            "payload_json",
            "created_at",
        }.issubset(event_columns)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_role_health_classifies_ready_task_unconsumed_without_mutation(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_task(
            conn,
            message_id="D42-ready-claude-stale",
            to_agent="Claude-Code",
            status="ready",
            updated_minutes_ago=90,
            task_id="APF-claude-implementation",
        )
    before = db_digest(db_path)

    snapshot = get_role_health_snapshot(config=config, project_root=tmp_path)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["read_only"] is True
    assert snapshot["mutation_actions"] == []
    claude = role_by_name(snapshot, "claude_implementer")
    assert claude["status"] == "stale"
    assert claude["primary_reason"] == "ready_task_unconsumed"
    assert claude["ready_task_count"] == 1
    assert claude["oldest_ready_task_age_seconds"] >= 45 * 60
    assert claude["next_action_role"] == "claude_implementer"


def test_role_health_classifies_claimed_stale_failed_human_and_queue_empty(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_task(
            conn,
            message_id="D42-claimed-codex-stale",
            to_agent="Codex",
            status="claimed",
            updated_minutes_ago=50,
            claimed_minutes_ago=50,
            task_id="planner-claimed-stale",
        )
        seed_task(
            conn,
            message_id="D42-reviewer-failed",
            to_agent="Reviewer",
            status="failed",
            updated_minutes_ago=5,
            task_id="reviewer-failed",
        )
        seed_task(
            conn,
            message_id="D42-human-gate",
            to_agent="Human",
            status="waiting_human",
            updated_minutes_ago=10,
            requires_human=True,
            task_id="human-gate",
        )

    snapshot = get_role_health_snapshot(config=config, project_root=tmp_path)

    planner = role_by_name(snapshot, "tech_lead_planner")
    assert planner["status"] == "stale"
    assert planner["primary_reason"] == "claimed_task_stale"
    assert planner["claimed_stale_count"] == 1

    reviewer = role_by_name(snapshot, "reviewer")
    assert reviewer["status"] == "failed"
    assert reviewer["primary_reason"] == "failed_task_present"
    assert reviewer["failed_task_count"] == 1

    git_steward = role_by_name(snapshot, "git_steward")
    assert git_steward["status"] == "queue_empty"
    assert git_steward["primary_reason"] == "queue_empty"

    blocker_reasons = {blocker["reason"] for blocker in snapshot["blockers"]}
    assert "human_gate_present" in blocker_reasons
    assert "claimed_task_stale" in blocker_reasons
    assert "failed_task_present" in blocker_reasons
    assert snapshot["current_blocking_classification"] == "mechanism_or_role_blocked"


def test_role_health_uses_agent_state_heartbeat_and_runner_disabled_reason(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    config["agents"]["Claude-Code"]["enabled"] = False
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO agent_states(
              role, display_name, adapter_type, enabled, health_status, health_reason,
              last_seen_at, current_task_id, detail_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tech_lead_planner",
                "Tech Lead / Planner",
                "codex_cli",
                1,
                "healthy",
                None,
                iso_minutes_ago(5),
                "D42-active-plan",
                json.dumps({"source": "fixture"}),
                iso_minutes_ago(5),
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_states(
              role, display_name, adapter_type, enabled, health_status, health_reason,
              last_seen_at, current_task_id, detail_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "reviewer",
                "Reviewer",
                "codex_review",
                1,
                "healthy",
                None,
                iso_minutes_ago(120),
                None,
                "{}",
                iso_minutes_ago(120),
            ),
        )

    snapshot = get_role_health_snapshot(config=config, project_root=tmp_path)

    planner = role_by_name(snapshot, "tech_lead_planner")
    assert planner["status"] == "healthy"
    assert planner["primary_reason"] == "recent_heartbeat"
    assert planner["current_task_id"] == "D42-active-plan"

    reviewer = role_by_name(snapshot, "reviewer")
    assert reviewer["status"] == "stale"
    assert reviewer["primary_reason"] == "no_recent_heartbeat"
    assert reviewer["heartbeat_age_seconds"] >= 30 * 60

    claude = role_by_name(snapshot, "claude_implementer")
    assert claude["status"] == "disabled"
    assert claude["primary_reason"] == "runner_disabled"


def test_role_health_preserves_valid_agent_state_reasons_and_downgrades_invalid_reason(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_agent_state(
            conn,
            role="tech_lead_planner",
            health_status="blocked",
            health_reason="scheduler_disabled",
        )
        seed_agent_state(
            conn,
            role="git_steward",
            health_status="waiting_peer",
            health_reason="git_steward_pending",
        )
        conn.execute("PRAGMA ignore_check_constraints = ON")
        seed_agent_state(
            conn,
            role="claude_implementer",
            health_status="blocked",
            health_reason="not_a_d4_2_reason",
        )
        conn.execute("PRAGMA ignore_check_constraints = OFF")

    snapshot = get_role_health_snapshot(config=config, project_root=tmp_path)

    planner = role_by_name(snapshot, "tech_lead_planner")
    assert planner["status"] == "blocked"
    assert planner["primary_reason"] == "scheduler_disabled"
    assert planner["next_action_role"] == "tech_lead_planner"

    git_steward = role_by_name(snapshot, "git_steward")
    assert git_steward["status"] == "waiting_peer"
    assert git_steward["primary_reason"] == "git_steward_pending"
    assert git_steward["next_action_role"] == "git_steward"

    claude = role_by_name(snapshot, "claude_implementer")
    assert claude["status"] == "blocked"
    assert claude["primary_reason"] is None


def test_role_health_reports_review_workflow_and_git_gate_pending_read_only(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_task(
            conn,
            message_id="D42-review-pending",
            to_agent="Reviewer",
            status="needs_review",
            updated_minutes_ago=15,
            task_id="review-pending",
        )
        now = iso_minutes_ago(0)
        conn.execute(
            """
            INSERT INTO workflow_runs(
              workflow_id, status, dry_run, idempotency_key, last_successful_step_id,
              real_agents_started, target_writes_performed, mcp_mutation_tools_exposed,
              artifact_root, created_at, updated_at
            ) VALUES ('D42-workflow-pending', 'waiting_peer', 1, 'D42-workflow-key', NULL, 0, 0, 0, ?, ?, ?)
            """,
            (str(tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts"), now, now),
        )
        conn.execute(
            """
            INSERT INTO pr_gate_status(
              plan_id, gate_state, required_checks_state, review_threads_state, review_decision,
              pr_url, read_only, mutation_actions_json, pr_mutation_performed, merge_performed,
              created_at, updated_at
            ) VALUES ('D42-plan-pending', 'pr_not_created_dry_run', 'not_polled', 'not_polled', NULL,
                      NULL, 1, '[]', 0, 0, ?, ?)
            """,
            (now, now),
        )

    before = db_digest(db_path)
    snapshot = get_role_health_snapshot(config=config, project_root=tmp_path)
    text = render_role_health_text(snapshot)
    after = db_digest(db_path)

    assert before == after
    reviewer = role_by_name(snapshot, "reviewer")
    assert reviewer["status"] == "waiting_peer"
    assert reviewer["primary_reason"] == "reviewer_pending"
    git_steward = role_by_name(snapshot, "git_steward")
    assert git_steward["status"] == "waiting_peer"
    assert git_steward["primary_reason"] == "git_steward_pending"
    assert snapshot["workflow_observations"]["pending_workflow_count"] == 1
    assert "Role health" in text
    assert "reviewer_pending" in text
    assert "git_steward_pending" in text
    assert "mutation_actions=[]" in text


def test_cli_role_health_and_role_health_snapshot_are_read_only(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    config_path = write_config(tmp_path, config)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_task(
            conn,
            message_id="D42-cli-ready",
            to_agent="Hermes",
            status="ready",
            updated_minutes_ago=90,
            task_id="advisor-ready-stale",
        )
    before = db_digest(db_path)

    role_health = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "role-health", "--config", str(config_path), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    role_health_snapshot = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "role-health-snapshot",
            "--config",
            str(config_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    after = db_digest(db_path)

    assert before == after
    assert role_health.returncode == 0, role_health.stderr
    payload = json.loads(role_health.stdout)
    assert payload["read_only"] is True
    assert payload["mutation_actions"] == []
    assert role_by_name(payload, "advisor_runner")["primary_reason"] == "ready_task_unconsumed"

    assert role_health_snapshot.returncode == 0, role_health_snapshot.stderr
    snapshot_payload = json.loads(role_health_snapshot.stdout)
    assert snapshot_payload["dashboard"]["cards"]
    assert snapshot_payload["dashboard"]["auto_repair_actions"] == []
    assert snapshot_payload["ready_for_real_agent_execution"] is False
    assert snapshot_payload["target_writes_performed"] is False


def test_dashboard_status_snapshot_includes_role_health_cards_without_mutation(tmp_path: Path) -> None:
    config = build_d42_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        seed_task(
            conn,
            message_id="D42-dashboard-ready",
            to_agent="Claude-Code",
            status="ready",
            updated_minutes_ago=90,
            task_id="dashboard-ready-stale",
        )
    before = db_digest(db_path)

    snapshot = get_status_snapshot(config=config, project_root=tmp_path, recent_events=5)
    text = render_status_text(snapshot)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["role_health"]["read_only"] is True
    assert snapshot["role_health"]["mutation_actions"] == []
    assert snapshot["role_health"]["dashboard"]["auto_repair_actions"] == []
    assert role_by_name(snapshot["role_health"], "claude_implementer")["primary_reason"] == "ready_task_unconsumed"
    assert "Role health" in text
    assert "claude_implementer" in text
    assert "ready_task_unconsumed" in text

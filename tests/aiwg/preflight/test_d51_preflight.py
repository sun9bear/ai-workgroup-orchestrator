from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.d5_preflight import evaluate_d5_preflight
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.state.database import connect_database, init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    config["state_db"] = "docs/ai-workgroup/state/tasks.sqlite"
    config["artifact_root"] = "docs/ai-workgroup/state/artifacts"
    config["workflow_contract"] = {
        "topology_path": str(PROJECT_ROOT / "docs" / "ai-workgroup" / "topology" / "aiwg.topology.v1.yaml"),
        "workflow_path": str(PROJECT_ROOT / "docs" / "ai-workgroup" / "workflows" / "apf-preview-funnel.workflow.v1.yaml"),
        "read_only": True,
        "validate_only": True,
    }
    config["d5_preflight"] = {
        "budget": {
            "max_budget_usd": 0,
            "requested_budget_usd": 0,
        },
        "lease": {
            "heartbeat_expected_seconds": 1200,
            "stale_after_seconds": 1800,
        },
    }
    return config


def write_config(project_root: Path, config: dict[str, Any] | None = None) -> Path:
    config_path = project_root / "aiwg.yaml"
    config_path.write_text(dump_config(config or build_test_config(project_root)), encoding="utf-8")
    return config_path


def protected_repo_digest(target_root: Path) -> tuple[tuple[str, int], ...]:
    if not target_root.exists():
        return ()
    return tuple(
        sorted(
            (str(path.relative_to(target_root)).replace("\\", "/"), path.stat().st_size)
            for path in target_root.rglob("*")
            if path.is_file()
        )
    )


def write_external_review_fixture(path: Path, *, dirty: bool = False) -> Path:
    payload = {
        "schema_version": "aiwg.external_review_fixture.v1",
        "read_only": not dirty,
        "mutation_actions": ["comment_pr"] if dirty else [],
        "sources": [
            {
                "id": "codex-d51-fixture",
                "source_type": "codex_report",
                "display_name": "CodeX fixture",
                "provider_ref": "local-fixture",
                "gate_state": "approved",
                "last_polled_at": "2026-06-07T00:00:00Z",
                "read_only": not dirty,
                "mutation_actions": ["comment_pr"] if dirty else [],
            }
        ],
        "items": [
            {
                "id": "codex-d51-non-blocking-note",
                "source_id": "codex-d51-fixture",
                "source_type": "codex_report",
                "item_state": "resolved",
                "feedback_category": "non_blocking",
                "title": "fixture ok",
                "body": "local read-only fixture",
                "resolved": True,
                "blocking": False,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def assert_d51_safety_flags_false(snapshot: dict[str, Any]) -> None:
    assert snapshot["dry_run"] is True
    assert snapshot["fake_only"] is True
    assert snapshot["ready_for_real_agent_execution"] is False
    assert snapshot["ready_for_protected_business_repository_write"] is False
    assert snapshot["target_writes_performed"] is False
    assert snapshot["mcp_mutation_tools_exposed"] is False
    assert snapshot["github_write_api_called"] is False
    assert snapshot["pr_comment_performed"] is False
    assert snapshot["pr_mutation_performed"] is False
    assert snapshot["created_fix_tasks"] is False
    assert snapshot["codex_automation_modified"] is False
    assert snapshot["git_push_performed"] is False
    assert snapshot["git_merge_performed"] is False
    assert snapshot["git_deploy_performed"] is False
    assert snapshot["real_agents_started"] is False
    assert snapshot["real_processes_started"] is False


def test_d51_schema_tables_exist_and_fail_closed(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 9
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "d5_budget_preflight" in tables
        assert "d5_checkpoint_lease_preflight" in tables
        assert "d5_external_review_fixture_ingest" in tables
        migrations = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        assert (9, "phase_d5_1_preflight_controls") in migrations

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO d5_budget_preflight(
                  id, preflight_run_id, role, workflow_id, max_budget_usd,
                  requested_budget_usd, consumed_budget_usd, status, dry_run, created_at
                ) VALUES (
                  'bad-budget', 'run', 'implementer', 'apf-preview-funnel', 0,
                  0, 1, 'within_budget', 1, '2026-01-01T00:00:00Z'
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO d5_checkpoint_lease_preflight(
                  id, preflight_run_id, workflow_id, checkpoint_id, role,
                  lease_state, real_lock_acquired, stale_recovery_performed,
                  reset_to_ready_performed, heartbeat_expected_seconds,
                  stale_after_seconds, created_at
                ) VALUES (
                  'bad-lease', 'run', 'apf-preview-funnel', 'implement', 'implementer',
                  'would_acquire', 1, 0, 0, 1200, 1800, '2026-01-01T00:00:00Z'
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO d5_external_review_fixture_ingest(
                  id, preflight_run_id, fixture_path, fixture_sha256, source_count,
                  item_count, gate_state, read_only, mutation_action_count,
                  github_write_api_called, pr_comment_performed, pr_mutation_performed,
                  created_fix_tasks, target_writes_performed, codex_automation_modified,
                  created_at
                ) VALUES (
                  'bad-fixture', 'run', 'fixture.json', 'abc', 1,
                  0, 'blocked', 0, 0,
                  0, 0, 0,
                  0, 0, 0,
                  '2026-01-01T00:00:00Z'
                )
                """
            )


def test_d51_snapshot_records_budget_lease_and_fixture_preflight_without_target_writes(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    target_root.mkdir()
    (target_root / "README.md").write_text("protected target repo sentinel\n", encoding="utf-8")
    fixture_path = write_external_review_fixture(project_root / "fixtures" / "codex-review.json")
    config = build_test_config(project_root)
    before = protected_repo_digest(target_root)

    snapshot = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
        include_d5_1=True,
        external_review_fixture=fixture_path,
    )

    assert snapshot["phase"] == "D5.1"
    assert snapshot["d5_scope"] == "D5.1-preflight"
    assert snapshot["status"] == "passed_dry_run"
    assert snapshot["d5_1_components"] == [
        "budget_preflight",
        "checkpoint_lease_heartbeat_stale_recovery_precheck",
        "external_review_fixture_ingest",
    ]
    assert_d51_safety_flags_false(snapshot)
    assert protected_repo_digest(target_root) == before

    budget = snapshot["budget_preflight"]
    assert budget["status"] == "within_budget"
    assert budget["total_requested_budget_usd"] == 0
    assert budget["total_consumed_budget_usd"] == 0
    assert budget["dry_run"] is True
    assert {row["role"] for row in budget["roles"]} >= {"implementer", "reviewer", "git_steward"}
    assert all(row["consumed_budget_usd"] == 0 for row in budget["roles"])

    lease = snapshot["checkpoint_lease_preflight"]
    assert lease["status"] == "checked"
    assert lease["checkpoint_count"] == 5
    assert lease["real_lock_acquired"] is False
    assert lease["stale_recovery_performed"] is False
    assert lease["reset_to_ready_performed"] is False
    assert {row["lease_state"] for row in lease["checkpoints"]} == {"would_acquire"}

    fixture = snapshot["external_review_fixture_ingest"]
    assert fixture["status"] == "ingested_read_only"
    assert fixture["gate_state"] == "approved"
    assert fixture["source_count"] == 1
    assert fixture["item_count"] == 1
    assert fixture["read_only"] is True
    assert fixture["mutation_actions"] == []
    assert fixture["fixture_sha256"]

    db_path = project_root / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    with connect_database(db_path) as conn:
        run_row = conn.execute(
            "SELECT status, dry_run, fake_only, ready_for_real_agent_execution, target_writes_performed "
            "FROM d5_preflight_runs WHERE id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone()
        assert run_row == ("passed_dry_run", 1, 1, 0, 0)
        assert conn.execute(
            "SELECT COUNT(*), SUM(consumed_budget_usd) FROM d5_budget_preflight WHERE preflight_run_id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone() == (5, 0.0)
        assert conn.execute(
            "SELECT COUNT(*), SUM(real_lock_acquired), SUM(reset_to_ready_performed) "
            "FROM d5_checkpoint_lease_preflight WHERE preflight_run_id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone() == (5, 0, 0)
        fixture_row = conn.execute(
            "SELECT gate_state, read_only, mutation_action_count, pr_mutation_performed "
            "FROM d5_external_review_fixture_ingest WHERE preflight_run_id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone()
        assert fixture_row == ("approved", 1, 0, 0)


def test_d51_budget_exceeded_blocks_but_consumes_zero_budget(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    config = build_test_config(project_root)
    config["d5_preflight"]["budget"]["max_budget_usd"] = 0
    config["d5_preflight"]["budget"]["requested_budget_usd"] = 1

    snapshot = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
        include_d5_1=True,
    )

    assert snapshot["status"] == "blocked"
    assert "budget_preflight.budget_exceeded" in snapshot["policy_denials"]
    assert snapshot["budget_preflight"]["status"] == "budget_exceeded"
    assert snapshot["budget_preflight"]["total_consumed_budget_usd"] == 0
    assert_d51_safety_flags_false(snapshot)


def test_d51_external_review_fixture_blocks_dirty_fixture_without_pr_mutation(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    fixture_path = write_external_review_fixture(project_root / "fixtures" / "dirty-review.json", dirty=True)
    config = build_test_config(project_root)

    snapshot = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
        include_d5_1=True,
        external_review_fixture=fixture_path,
    )

    assert snapshot["status"] == "blocked"
    assert "external_review_fixture.mutation_actions_present" in snapshot["policy_denials"]
    fixture = snapshot["external_review_fixture_ingest"]
    assert fixture["status"] == "blocked"
    assert fixture["gate_state"] == "blocked"
    assert fixture["read_only"] is True
    assert fixture["fixture_declared_read_only"] is False
    assert fixture["mutation_action_count"] == 1
    assert fixture["mutation_actions"] == ["comment_pr"]
    assert fixture["pr_comment_performed"] is False
    assert fixture["pr_mutation_performed"] is False
    assert fixture["github_write_api_called"] is False
    assert fixture["target_writes_performed"] is False
    assert fixture["created_fix_tasks"] is False
    assert_d51_safety_flags_false(snapshot)


def test_d51_cli_fail_on_blocked_returns_3_but_default_remains_compatible(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    config = build_test_config(project_root)
    config["policy"]["allow_real_agents"] = True
    config_path = write_config(project_root, config)

    base_cmd = [
        sys.executable,
        "-m",
        "aiwg.cli",
        "d5-preflight",
        "--config",
        str(config_path),
        "--workflow-id",
        "apf-preview-funnel",
        "--target-root",
        str(target_root),
        "--dry-run",
        "--json",
    ]

    compatible = subprocess.run(
        base_cmd,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert compatible.returncode == 0
    assert json.loads(compatible.stdout)["status"] == "blocked"

    fail_on_blocked = subprocess.run(
        [*base_cmd, "--fail-on-blocked"],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert fail_on_blocked.returncode == 3
    assert json.loads(fail_on_blocked.stdout)["status"] == "blocked"


def test_d51_status_dashboard_includes_latest_preflight_components(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    fixture_path = write_external_review_fixture(project_root / "fixtures" / "codex-review.json")
    config = build_test_config(project_root)

    preflight = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
        include_d5_1=True,
        external_review_fixture=fixture_path,
    )
    snapshot = get_status_snapshot(config=config, project_root=project_root)
    text = render_status_text(snapshot)

    assert snapshot["d5_preflight"]["preflight_run_id"] == preflight["preflight_run_id"]
    assert snapshot["d5_preflight"]["phase"] == "D5.1"
    assert snapshot["d5_preflight"]["budget_preflight"]["status"] == "within_budget"
    assert snapshot["d5_preflight"]["checkpoint_lease_preflight"]["checkpoint_count"] == 5
    assert snapshot["d5_preflight"]["external_review_fixture_ingest"]["gate_state"] == "approved"
    assert_d51_safety_flags_false(snapshot["d5_preflight"])
    assert "D5 preflight" in text
    assert "scope=D5.1-preflight" in text
    assert "budget=within_budget" in text
    assert "checkpoint_lease=checked" in text
    assert "external_review_fixture=approved" in text

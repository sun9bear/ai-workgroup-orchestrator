from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config
from aiwg.d5_preflight import evaluate_d5_preflight
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
        "budget": {"max_budget_usd": 0, "requested_budget_usd": 0},
        "lease": {"heartbeat_expected_seconds": 1200, "stale_after_seconds": 1800},
    }
    return config


def write_external_review_fixture(
    path: Path,
    *,
    declared_read_only: bool = True,
    mutation_actions: list[Any] | None = None,
) -> Path:
    actions = list(mutation_actions or [])
    payload = {
        "schema_version": "aiwg.external_review_fixture.v1",
        "read_only": declared_read_only,
        "mutation_actions": actions,
        "sources": [
            {
                "id": "codex-d52-fixture",
                "source_type": "codex_report",
                "display_name": "CodeX D5.2 fixture",
                "provider_ref": "local-fixture",
                "gate_state": "approved",
                "last_polled_at": "2026-06-07T00:00:00Z",
                "read_only": declared_read_only,
                "mutation_actions": actions,
            }
        ],
        "items": [
            {
                "id": "codex-d52-note",
                "source_id": "codex-d52-fixture",
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


def insert_parent_preflight_run(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO d5_preflight_runs(
          id, workflow_id, status, dry_run, fake_only,
          ready_for_real_agent_execution, ready_for_protected_business_repository_write,
          target_writes_performed, mcp_mutation_tools_exposed, github_write_api_called,
          pr_comment_performed, pr_mutation_performed, created_fix_tasks,
          codex_automation_modified, git_push_performed, git_merge_performed,
          git_deploy_performed, real_agents_started, real_processes_started,
          artifact_path, artifact_sha256, created_at, updated_at
        ) VALUES (
          'd52-parent', 'apf-preview-funnel', 'blocked', 1, 1,
          0, 0,
          0, 0, 0,
          0, 0, 0,
          0, 0, 0,
          0, 0, 0,
          'docs/ai-workgroup/state/artifacts/phase-d5-preflight/d52-parent.json',
          'abc', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """
    )


def test_d52_schema_tracks_fixture_declared_read_only_and_blocks_mutation_count_semantically(
    tmp_path: Path,
) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 10
        migrations = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        assert (10, "phase_d5_2_preflight_hardening") in migrations
        columns = {row[1] for row in conn.execute("PRAGMA table_info(d5_external_review_fixture_ingest)").fetchall()}
        assert "fixture_declared_read_only" in columns
        insert_parent_preflight_run(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO d5_external_review_fixture_ingest(
                  id, preflight_run_id, fixture_path, fixture_sha256, source_count,
                  item_count, status, gate_state, read_only, fixture_declared_read_only,
                  mutation_action_count, mutation_actions_json, github_write_api_called,
                  pr_comment_performed, pr_mutation_performed, created_fix_tasks,
                  target_writes_performed, codex_automation_modified, created_at
                ) VALUES (
                  'bad-mutation-count', 'd52-parent', 'fixture.json', 'abc', 1,
                  0, 'ingested_read_only', 'approved', 1, 1,
                  1, '[""comment_pr""]', 0,
                  0, 0, 0,
                  0, 0, '2026-01-01T00:00:00Z'
                )
                """
            )

        conn.execute(
            """
            INSERT INTO d5_external_review_fixture_ingest(
              id, preflight_run_id, fixture_path, fixture_sha256, source_count,
              item_count, status, gate_state, read_only, fixture_declared_read_only,
              mutation_action_count, mutation_actions_json, github_write_api_called,
              pr_comment_performed, pr_mutation_performed, created_fix_tasks,
              target_writes_performed, codex_automation_modified, created_at
            ) VALUES (
              'blocked-dirty-fixture', 'd52-parent', 'fixture.json', 'abc', 1,
              0, 'blocked', 'blocked', 1, 0,
              1, '[""comment_pr""]', 0,
              0, 0, 0,
              0, 0, '2026-01-01T00:00:00Z'
            )
            """
        )
        assert conn.execute(
            "SELECT status, read_only, fixture_declared_read_only, mutation_action_count "
            "FROM d5_external_review_fixture_ingest WHERE id='blocked-dirty-fixture'"
        ).fetchone() == ("blocked", 1, 0, 1)


def test_d52_dirty_fixture_records_declared_read_only_false_without_performing_mutation(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    fixture_path = write_external_review_fixture(
        project_root / "fixtures" / "dirty-review.json",
        declared_read_only=False,
        mutation_actions=["comment_pr"],
    )
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
    assert "external_review_fixture.declared_not_read_only" in snapshot["policy_denials"]
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

    db_path = project_root / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    with connect_database(db_path) as conn:
        assert conn.execute(
            "SELECT status, gate_state, read_only, fixture_declared_read_only, mutation_action_count "
            "FROM d5_external_review_fixture_ingest WHERE preflight_run_id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone() == ("blocked", "blocked", 1, 0, 1)


def test_d52_unified_evidence_path_guard_rejects_non_artifact_roots(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    config = build_test_config(project_root)
    config["artifact_root"] = "tmp-artifacts"

    with pytest.raises(ValueError, match="artifact_root_outside_orchestrator_artifacts"):
        evaluate_d5_preflight(
            config=config,
            project_root=project_root,
            workflow_id="apf-preview-funnel",
            target_root=target_root,
            dry_run=True,
            include_d5_1=True,
        )


def test_d52_unified_evidence_path_guard_rejects_target_overlap(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = project_root / "docs" / "ai-workgroup" / "state" / "artifacts" / "target-overlap"
    project_root.mkdir()
    target_root.mkdir(parents=True)
    config = build_test_config(project_root)
    config["artifact_root"] = str(target_root)

    with pytest.raises(ValueError, match="artifact_root_overlaps_target_root"):
        evaluate_d5_preflight(
            config=config,
            project_root=project_root,
            workflow_id="apf-preview-funnel",
            target_root=target_root,
            dry_run=True,
            include_d5_1=True,
        )

from __future__ import annotations

import hashlib
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


def assert_d50_safety_flags_false(snapshot: dict[str, Any]) -> None:
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


def test_d50_schema_tables_exist_and_fail_closed(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 8
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "d5_preflight_runs" in tables
        assert "d5_artifact_provenance" in tables
        migrations = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        assert (8, "phase_d5_preflight_minimal") in migrations

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO d5_preflight_runs(
                  id, workflow_id, status, dry_run, fake_only,
                  ready_for_real_agent_execution, target_writes_performed,
                  mcp_mutation_tools_exposed, github_write_api_called,
                  pr_comment_performed, pr_mutation_performed, created_fix_tasks,
                  codex_automation_modified, git_push_performed, git_merge_performed,
                  git_deploy_performed, real_agents_started, real_processes_started,
                  artifact_path, created_at, updated_at
                ) VALUES (
                  'bad-real-ready', 'apf-preview-funnel', 'passed_dry_run', 1, 1,
                  1, 0, 0, 0,
                  0, 0, 0,
                  0, 0, 0,
                  0, 0, 0,
                  'docs/ai-workgroup/state/artifacts/phase-d5-preflight/bad.json',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO d5_artifact_provenance(
                  id, preflight_run_id, artifact_kind, artifact_path, artifact_sha256,
                  origin_component, workflow_id, under_orchestrator_root, under_target_root, created_at
                ) VALUES (
                  'bad-target-artifact', 'run', 'd5_preflight_report', 'D:/target/report.json',
                  'abc', 'd5_preflight', 'apf-preview-funnel', 0, 1, '2026-01-01T00:00:00Z'
                )
                """
            )


def test_d50_snapshot_writes_orchestrator_only_artifact_and_provenance(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    target_root.mkdir()
    (target_root / "README.md").write_text("protected target repo sentinel\n", encoding="utf-8")
    config = build_test_config(project_root)
    before = protected_repo_digest(target_root)

    snapshot = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
    )

    assert snapshot["schema_version"] == "aiwg.d5_preflight_result.v1"
    assert snapshot["phase"] == "D5.0"
    assert snapshot["d5_scope"] == "D5.0-minimal"
    assert snapshot["status"] == "passed_dry_run"
    assert snapshot["deferred_to_d5_1"] == [
        "budget_preflight",
        "checkpoint_lease_heartbeat_stale_recovery",
        "external_review_fixture_ingest",
    ]
    assert_d50_safety_flags_false(snapshot)
    assert protected_repo_digest(target_root) == before

    artifact_path = Path(snapshot["artifact_path"])
    assert artifact_path.exists()
    assert artifact_path.resolve().is_relative_to(project_root.resolve())
    assert not artifact_path.resolve().is_relative_to(target_root.resolve())

    provenance = snapshot["artifact_provenance"]
    assert provenance["artifact_kind"] == "d5_preflight_report"
    assert provenance["under_orchestrator_root"] is True
    assert provenance["under_target_root"] is False
    assert len(provenance["artifact_sha256"]) == 64
    assert provenance["artifact_sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    db_path = project_root / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    with connect_database(db_path) as conn:
        run_row = conn.execute(
            "SELECT status, dry_run, fake_only, ready_for_real_agent_execution, target_writes_performed "
            "FROM d5_preflight_runs WHERE id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone()
        assert run_row == ("passed_dry_run", 1, 1, 0, 0)
        provenance_row = conn.execute(
            "SELECT artifact_path, artifact_sha256, under_orchestrator_root, under_target_root "
            "FROM d5_artifact_provenance WHERE preflight_run_id = ?",
            (snapshot["preflight_run_id"],),
        ).fetchone()
        assert provenance_row == (
            str(artifact_path),
            provenance["artifact_sha256"],
            1,
            0,
        )


def test_d50_snapshot_blocks_unsafe_policy_but_keeps_mutation_flags_false(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "target"
    project_root.mkdir()
    target_root.mkdir()
    config = build_test_config(project_root)
    config["policy"]["allow_real_agents"] = True
    config["policy"]["allow_push"] = True

    snapshot = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
    )

    assert snapshot["status"] == "blocked"
    assert "policy.allow_real_agents" in snapshot["policy_denials"]
    assert "policy.allow_push" in snapshot["policy_denials"]
    assert_d50_safety_flags_false(snapshot)


def test_d50_cli_requires_dry_run_and_emits_json_snapshot(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    target_root.mkdir()
    config_path = write_config(project_root)

    missing_dry_run = subprocess.run(
        [
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
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert missing_dry_run.returncode == 2
    assert "--dry-run is required" in missing_dry_run.stdout

    completed = subprocess.run(
        [
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
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    snapshot = json.loads(completed.stdout)
    assert snapshot["status"] == "passed_dry_run"
    assert snapshot["phase"] == "D5.0"
    assert_d50_safety_flags_false(snapshot)


def test_d50_status_dashboard_includes_latest_preflight_read_only(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    target_root.mkdir()
    (target_root / "README.md").write_text("protected target repo sentinel\n", encoding="utf-8")
    config = build_test_config(project_root)
    before = protected_repo_digest(target_root)

    preflight = evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
    )
    snapshot = get_status_snapshot(config=config, project_root=project_root)
    text = render_status_text(snapshot)

    assert protected_repo_digest(target_root) == before
    assert snapshot["d5_preflight"]["preflight_run_id"] == preflight["preflight_run_id"]
    assert snapshot["d5_preflight"]["status"] == "passed_dry_run"
    assert_d50_safety_flags_false(snapshot["d5_preflight"])
    assert "D5 preflight" in text
    assert "ready_for_real_agent_execution=false" in text
    assert "target_writes_performed=false" in text

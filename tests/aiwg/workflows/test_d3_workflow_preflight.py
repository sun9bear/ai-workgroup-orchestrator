from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config, dump_config
from aiwg.state.database import resolve_db_path
from aiwg.workflow_preflight import get_workflow_status, plan_workflow_dry_run

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    return config


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def write_config(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def workflow_step(step_id: str, key: str, target_root: Path, **extra: Any) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "adapter": "fake",
        "idempotency_key": key,
        "target_root": str(target_root),
        "candidate_paths": ["src/app.py"],
        **extra,
    }


def assert_no_business_artifacts(target_root: Path) -> None:
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts").exists()
    assert not list(target_root.rglob("workflow-ledger.sqlite"))
    assert not list(target_root.rglob("workflow-*.json"))


def test_workflow_dry_run_records_intent_before_fake_output_and_no_real_side_effects(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    result = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-intent-output",
        steps=[workflow_step("gate", "d3-intent-output-key", target_root)],
    )

    assert result.status == "completed"
    assert result.workflow_id == "D3-wf-intent-output"
    assert result.dispatched_steps == 1
    assert result.real_agents_started is False
    assert result.target_writes_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert result.artifact_root.is_relative_to(tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts")
    assert_no_business_artifacts(target_root)

    db_path = resolve_db_path(config, tmp_path)
    assert db_rows(
        db_path,
        """
        SELECT workflow_id, status, dry_run, real_agents_started,
               target_writes_performed, mcp_mutation_tools_exposed,
               last_successful_step_id
        FROM workflow_runs
        """,
    ) == [("D3-wf-intent-output", "completed", 1, 0, 0, 0, "gate")]

    intent_row = db_rows(
        db_path,
        "SELECT id, workflow_id, step_id, idempotency_key FROM workflow_step_intents",
    )[0]
    output_row = db_rows(
        db_path,
        "SELECT intent_id, workflow_id, step_id, status, artifact_path FROM workflow_step_outputs",
    )[0]
    assert output_row[:4] == (intent_row[0], "D3-wf-intent-output", "gate", "succeeded")
    output_path = Path(output_row[4])
    assert output_path.exists()
    assert output_path.is_relative_to(result.artifact_root)
    output_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert output_payload["schema_version"] == "aiwg.workflow_step_output.v1"
    assert output_payload["workflow_id"] == "D3-wf-intent-output"
    assert output_payload["step_id"] == "gate"
    assert output_payload["intent_id"] == intent_row[0]
    assert output_payload["adapter"] == "fake"
    assert output_payload["fake_adapter_only"] is True
    assert output_payload["real_agents_started"] is False
    assert output_payload["target_writes_performed"] is False
    assert output_payload["mcp_mutation_tools_exposed"] is False

    assert db_rows(
        db_path,
        "SELECT type FROM events WHERE task_id = ? ORDER BY id",
        ("D3-wf-intent-output",),
    ) == [
        ("workflow_run_started",),
        ("workflow_step_intent_recorded",),
        ("workflow_step_fake_output_written",),
        ("workflow_run_completed",),
    ]


def test_failed_step_resumes_from_last_successful_gate_without_redispatching_prior_step(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    db_path = resolve_db_path(config, tmp_path)

    first = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-resume",
        steps=[
            workflow_step("s1", "d3-resume-s1", target_root),
            workflow_step("s2", "d3-resume-s2", target_root, simulate_failure_before_output=True),
        ],
    )

    assert first.status == "failed"
    assert first.last_successful_step_id == "s1"
    assert first.dispatched_steps == 1
    assert db_rows(db_path, "SELECT COUNT(*) FROM workflow_step_outputs WHERE step_id = 's1'") == [(1,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM workflow_step_intents WHERE step_id = 's2'") == [(1,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM workflow_step_outputs WHERE step_id = 's2'") == [(0,)]
    s2_intent_id = db_rows(db_path, "SELECT id FROM workflow_step_intents WHERE step_id = 's2'")[0][0]

    second = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-resume",
        steps=[
            workflow_step("s1", "d3-resume-s1", target_root),
            workflow_step("s2", "d3-resume-s2", target_root),
        ],
    )

    assert second.status == "completed"
    assert second.last_successful_step_id == "s2"
    assert second.dispatched_steps == 1
    assert db_rows(db_path, "SELECT COUNT(*) FROM workflow_step_outputs WHERE step_id = 's1'") == [(1,)]
    assert db_rows(db_path, "SELECT intent_id FROM workflow_step_outputs WHERE step_id = 's2'") == [(s2_intent_id,)]
    assert_no_business_artifacts(target_root)


def test_duplicate_idempotency_key_does_not_redispatch_fake_step(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    db_path = resolve_db_path(config, tmp_path)

    first = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-first",
        steps=[workflow_step("gate", "d3-duplicate-key", target_root)],
    )
    second = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-second",
        steps=[workflow_step("gate", "d3-duplicate-key", target_root)],
    )

    assert first.status == "completed"
    assert second.status == "duplicate_idempotency_key"
    assert second.dispatched_steps == 0
    assert second.duplicate_idempotency_key == "d3-duplicate-key"
    assert db_rows(
        db_path,
        """
        SELECT COUNT(*)
        FROM workflow_step_outputs AS output
        JOIN workflow_step_intents AS intent ON intent.id = output.intent_id
        WHERE intent.idempotency_key = ?
        """,
        ("d3-duplicate-key",),
    ) == [(1,)]
    assert_no_business_artifacts(target_root)


def test_same_workflow_step_changed_idempotency_key_fails_closed_without_ledger_rewrite(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    db_path = resolve_db_path(config, tmp_path)

    first = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-key-change",
        steps=[workflow_step("gate", "d3-key-old", target_root)],
    )
    assert first.status == "completed"
    events_before = db_rows(db_path, "SELECT COUNT(*) FROM events WHERE task_id = ?", ("D3-wf-key-change",))[0][0]

    second = plan_workflow_dry_run(
        config=config,
        project_root=tmp_path,
        workflow_id="D3-wf-key-change",
        steps=[workflow_step("gate", "d3-key-new", target_root)],
    )

    assert second.status == "idempotency_key_mismatch"
    assert second.dispatched_steps == 0
    assert second.error == "idempotency_key_mismatch:gate"
    assert db_rows(db_path, "SELECT status FROM workflow_runs WHERE workflow_id = ?", ("D3-wf-key-change",)) == [("completed",)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM events WHERE task_id = ?", ("D3-wf-key-change",)) == [(events_before,)]
    assert db_rows(db_path, "SELECT idempotency_key FROM workflow_steps WHERE workflow_id = ? AND step_id = ?", ("D3-wf-key-change", "gate")) == [("d3-key-old",)]
    assert db_rows(db_path, "SELECT idempotency_key FROM workflow_step_intents WHERE workflow_id = ? AND step_id = ?", ("D3-wf-key-change", "gate")) == [("d3-key-old",)]
    assert db_rows(db_path, "SELECT idempotency_key FROM workflow_step_outputs WHERE workflow_id = ? AND step_id = ?", ("D3-wf-key-change", "gate")) == [("d3-key-old",)]
    assert_no_business_artifacts(target_root)


def test_misconfigured_artifact_root_under_target_root_fails_closed_before_writing(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    config["artifact_root"] = str(target_root / "docs" / "ai-workgroup" / "state" / "artifacts")

    try:
        plan_workflow_dry_run(
            config=config,
            project_root=tmp_path,
            workflow_id="D3-wf-bad-artifact-root",
            steps=[workflow_step("gate", "d3-bad-artifact-root", target_root)],
        )
    except ValueError as exc:
        assert "artifact_root_outside_orchestrator_artifacts" in str(exc)
    else:  # pragma: no cover - RED guard
        raise AssertionError("misconfigured artifact root did not fail closed")

    assert_no_business_artifacts(target_root)


def test_misconfigured_state_db_under_target_root_fails_closed_before_writing(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    config["state_db"] = str(target_root / "docs" / "ai-workgroup" / "state" / "tasks.sqlite")

    try:
        plan_workflow_dry_run(
            config=config,
            project_root=tmp_path,
            workflow_id="D3-wf-bad-state-db",
            steps=[workflow_step("gate", "d3-bad-state-db", target_root)],
        )
    except ValueError as exc:
        assert "state_db_outside_orchestrator_state" in str(exc)
    else:  # pragma: no cover - RED guard
        raise AssertionError("misconfigured state_db did not fail closed")

    assert not list(target_root.rglob("tasks.sqlite*"))
    assert_no_business_artifacts(target_root)


def test_target_root_nested_under_step_artifact_path_fails_closed_before_writing(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    workflow_id = "D3-wf-target-nested"
    target_root = tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "workflows" / workflow_id / "gate"

    try:
        plan_workflow_dry_run(
            config=config,
            project_root=tmp_path,
            workflow_id=workflow_id,
            steps=[workflow_step("gate", "d3-target-nested", target_root)],
        )
    except ValueError as exc:
        assert "artifact_root_overlaps_target_root" in str(exc)
    else:  # pragma: no cover - RED guard
        raise AssertionError("nested target_root did not fail closed")

    assert not target_root.exists()


def test_workflow_plan_and_status_cli_are_dry_run_json_surfaces(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    plan = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "workflow-plan",
            "--config",
            str(config_path),
            "--workflow-id",
            "D3-cli-wf",
            "--step",
            "cli-gate",
            "--idempotency-key",
            "D3-cli-key",
            "--target-root",
            str(target_root),
            "--dry-run",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert plan.returncode == 0, plan.stderr
    plan_payload = json.loads(plan.stdout)
    assert plan_payload["status"] == "completed"
    assert plan_payload["workflow_id"] == "D3-cli-wf"
    assert plan_payload["real_agents_started"] is False
    assert plan_payload["target_writes_performed"] is False
    assert plan_payload["mcp_mutation_tools_exposed"] is False

    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "workflow-status",
            "--config",
            str(config_path),
            "--workflow-id",
            "D3-cli-wf",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["workflow_id"] == "D3-cli-wf"
    assert status_payload["status"] == "completed"
    assert status_payload["steps"] == [
        {
            "step_id": "cli-gate",
            "status": "succeeded",
            "idempotency_key": "D3-cli-key",
            "output_status": "succeeded",
        }
    ]
    assert get_workflow_status(config=config_path and build_test_config(tmp_path), project_root=tmp_path, workflow_id="D3-cli-wf").status == "completed"
    assert_no_business_artifacts(target_root)

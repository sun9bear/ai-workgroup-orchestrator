from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APPROVAL_SCHEMA = "aiwg.phase_d1_approval_envelope.v1"
ROLLBACK_SCHEMA = "aiwg.phase_d1_rollback_plan.v1"
LEDGER_FILENAME = "write-gate-ledger.sqlite"


def safety_policy() -> dict:
    return {
        "safe_mode": True,
        "allow_write": False,
        "allow_real_agents": False,
        "allow_external_agents": False,
        "allow_real_adapter_dispatch": False,
        "allow_real_process_execution": False,
        "allow_push": False,
        "allow_merge": False,
        "allow_deploy": False,
        "allow_modify_codex_automations": False,
        "allow_secret_access": False,
        "allow_network_write": False,
        "allow_destructive_commands": False,
    }


def make_config(orchestrator_root: Path) -> dict:
    return {
        "project_root": ".",
        "orchestrator_root": str(orchestrator_root),
        "artifact_root": "docs/ai-workgroup/state/artifacts",
        "policy": safety_policy(),
    }


def make_candidate(target_root: Path, *, key_suffix: str = "001") -> dict:
    return {
        "schema_version": "aiwg.phase_d1_candidate_write_intent.v1",
        "phase": "D1",
        "task_id": f"D1-task-{key_suffix}",
        "message_id": f"D1-msg-{key_suffix}",
        "target_root": str(target_root),
        "writes": [
            {
                "path": "src/allowed.txt",
                "operation": "write_text",
                "content_sha256": "0" * 64,
                "content_preview": "[REDACTED]",
            }
        ],
    }


def artifact_base(orchestrator_root: Path) -> Path:
    return orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts"


def artifact_dir(orchestrator_root: Path) -> Path:
    return artifact_base(orchestrator_root) / "phase-d1-dry-run-write-gate"


def ledger_path(orchestrator_root: Path) -> Path:
    return artifact_dir(orchestrator_root) / LEDGER_FILENAME


def write_rollback_plan(orchestrator_root: Path, *, key_suffix: str = "001") -> Path:
    rollback_path = artifact_base(orchestrator_root) / "phase-d21-test" / f"rollback-{key_suffix}.json"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path.write_text(
        json.dumps(
            {
                "schema_version": ROLLBACK_SCHEMA,
                "phase": "D1",
                "task_id": f"D1-task-{key_suffix}",
                "message_id": f"D1-msg-{key_suffix}",
                "target_writes_performed": False,
                "protected_business_repository_write_performed": False,
                "rollback_steps": ["dry-run only; no target write to roll back"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return rollback_path


def make_envelope(orchestrator_root: Path, *, key_suffix: str = "001") -> dict:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "schema_version": APPROVAL_SCHEMA,
        "phase": "D1",
        "task_id": f"D1-task-{key_suffix}",
        "message_id": f"D1-msg-{key_suffix}",
        "operator": "Human",
        "approved_paths": ["src/**"],
        "forbidden_paths": [],
        "rollback_plan_path": str(write_rollback_plan(orchestrator_root, key_suffix=key_suffix)),
        "verification_commands": ["python -m pytest -q tests/aiwg/write_gate"],
        "expires_at": expires_at.isoformat(),
        "idempotency_key": f"d21-key-{key_suffix}",
    }


def fetch_rows(db_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def assert_no_business_artifacts(target_root: Path) -> None:
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts").exists()
    assert not list(target_root.rglob("audit-D1-msg-*.json"))
    assert not list(target_root.rglob(LEDGER_FILENAME))
    assert not list(target_root.rglob("idempotency-index.json"))


def test_api_fails_closed_without_explicit_or_configured_orchestrator_root(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    config = make_config(orchestrator_root)
    config.pop("orchestrator_root")

    with pytest.raises(ValueError, match="orchestrator_root_required"):
        evaluate_write_gate_dry_run(
            config=config,
            project_root=target_root,
            candidate_intent=make_candidate(target_root, key_suffix="fail-closed"),
            approval_envelope=make_envelope(orchestrator_root, key_suffix="fail-closed"),
        )

    assert_no_business_artifacts(target_root)
    assert not artifact_dir(orchestrator_root).exists()


def test_failed_sqlite_evaluation_does_not_leave_orphan_final_audit_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    def raise_after_audit_is_prepared(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("forced evaluation insert failure")

    monkeypatch.setattr(write_gate, "_record_sqlite_evaluation", raise_after_audit_is_prepared)

    with pytest.raises(sqlite3.OperationalError, match="forced evaluation insert failure"):
        write_gate.evaluate_write_gate_dry_run(
            config=make_config(orchestrator_root),
            project_root=target_root,
            orchestrator_root=orchestrator_root,
            candidate_intent=make_candidate(target_root, key_suffix="atomicity"),
            approval_envelope=make_envelope(orchestrator_root, key_suffix="atomicity"),
        )

    gate_artifact_dir = artifact_dir(orchestrator_root)
    assert not list(gate_artifact_dir.glob("audit-D1-msg-atomicity-*.json"))
    assert not list(gate_artifact_dir.glob("*.tmp"))
    assert not list(gate_artifact_dir.glob("*.pending"))
    assert_no_business_artifacts(target_root)


def test_legacy_json_idempotency_index_is_quarantined_and_not_used_as_active_state(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    gate_artifact_dir = artifact_dir(orchestrator_root)
    gate_artifact_dir.mkdir(parents=True)
    legacy_index = gate_artifact_dir / "idempotency-index.json"
    legacy_payload = {"schema_version": "aiwg.phase_d1_idempotency_index.v1", "keys": {"old": {"decision": "dry_run_only"}}}
    legacy_index.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="quarantine"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="quarantine"),
    )

    assert result.decision == "dry_run_only"
    assert not legacy_index.exists()
    quarantined = sorted((gate_artifact_dir / "legacy").glob("idempotency-index*.json"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text(encoding="utf-8")) == legacy_payload
    assert ledger_path(orchestrator_root).exists()
    assert len(fetch_rows(ledger_path(orchestrator_root), "SELECT * FROM write_gate_idempotency")) == 1
    assert_no_business_artifacts(target_root)


def test_cli_ledger_smoke_uses_config_directory_orchestrator_root_when_project_root_is_target(tmp_path: Path) -> None:
    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    config_path = orchestrator_root / "aiwg.yaml"
    candidate_path = tmp_path / "candidate.json"
    envelope_path = tmp_path / "envelope.json"

    config = make_config(orchestrator_root)
    config.pop("orchestrator_root")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    candidate_path.write_text(json.dumps(make_candidate(target_root, key_suffix="cli-ledger"), ensure_ascii=False), encoding="utf-8")
    envelope_path.write_text(json.dumps(make_envelope(orchestrator_root, key_suffix="cli-ledger"), ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "write-gate-dry-run",
            "--config",
            str(config_path),
            "--project-root",
            str(target_root),
            "--candidate",
            str(candidate_path),
            "--envelope",
            str(envelope_path),
            "--json",
            "--fail-on-deny",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["decision"] == "dry_run_only"
    assert Path(payload["audit_artifact_path"]).resolve(strict=False).is_relative_to(artifact_base(orchestrator_root).resolve(strict=False))
    db_path = ledger_path(orchestrator_root)
    assert db_path.exists()
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_evaluations")) == 1
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_idempotency WHERE idempotency_key = ?", ("d21-key-cli-ledger",))) == 1
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_rollback_artifacts")) == 1
    assert_no_business_artifacts(target_root)

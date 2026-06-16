from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
D0_ARTIFACT_PATH = PROJECT_ROOT / "docs/ai-workgroup/state/artifacts/phase-d0-controlled-write-gate-design/write-gate-design.json"


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


def make_config(tmp_path: Path) -> dict:
    return {
        "project_root": ".",
        "orchestrator_root": str(tmp_path),
        "artifact_root": "docs/ai-workgroup/state/artifacts",
        "policy": safety_policy(),
    }


def make_candidate(target_root: Path, *, path: str = "src/allowed.txt") -> dict:
    return {
        "schema_version": "aiwg.phase_d1_candidate_write_intent.v1",
        "phase": "D1",
        "task_id": "D1-task-001",
        "message_id": "D1-msg-001",
        "target_root": str(target_root),
        "writes": [
            {
                "path": path,
                "operation": "write_text",
                "content_sha256": "0" * 64,
                "content_preview": "[REDACTED]",
            }
        ],
    }


def make_envelope(tmp_path: Path, *, key: str = "d1-key-001", expires_delta: timedelta | None = None) -> dict:
    rollback_path = tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d1-test" / "rollback.json"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_payload = {
        "schema_version": "aiwg.phase_d1_rollback_plan.v1",
        "phase": "D1",
        "task_id": "D1-task-001",
        "message_id": "D1-msg-001",
        "target_writes_performed": False,
        "protected_business_repository_write_performed": False,
        "rollback_steps": ["restore from pre-write hashes"],
    }
    rollback_path.write_text(json.dumps(rollback_payload, ensure_ascii=False), encoding="utf-8")
    expires_at = datetime.now(timezone.utc) + (expires_delta if expires_delta is not None else timedelta(hours=1))
    return {
        "schema_version": "aiwg.phase_d1_approval_envelope.v1",
        "phase": "D1",
        "task_id": "D1-task-001",
        "message_id": "D1-msg-001",
        "operator": "Human",
        "approved_paths": ["src/**"],
        "forbidden_paths": ["src/forbidden/**"],
        "rollback_plan_path": str(rollback_path),
        "verification_commands": ["python -m pytest -q"],
        "expires_at": expires_at.isoformat(),
        "idempotency_key": key,
    }


def read_audit(path: Path) -> dict:
    assert path.exists(), path
    return json.loads(path.read_text(encoding="utf-8"))


def test_d0_artifact_safety_switches_include_d1_boundary_fields() -> None:
    artifact = json.loads(D0_ARTIFACT_PATH.read_text(encoding="utf-8"))
    switches = artifact["safety_switches"]
    for key in ("allow_secret_access", "allow_network_write", "allow_destructive_commands"):
        assert switches[key] is False


def test_missing_envelope_denies_and_writes_audit_without_target_write(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    candidate = make_candidate(target_root)

    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=candidate,
        approval_envelope=None,
    )

    assert result.decision == "deny"
    assert result.target_writes_performed is False
    assert "missing_approval_envelope" in result.reasons
    assert not (target_root / "src" / "allowed.txt").exists()
    audit = read_audit(result.audit_artifact_path)
    assert audit["schema_version"] == "aiwg.phase_d1_write_gate_audit.v1"
    assert audit["decision"] == "deny"
    assert audit["target_writes_performed"] is False


def test_valid_envelope_returns_dry_run_only_and_does_not_modify_target(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    (target_root / "src").mkdir(parents=True)
    existing = target_root / "src" / "allowed.txt"
    existing.write_text("before\n", encoding="utf-8")

    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=make_candidate(target_root),
        approval_envelope=make_envelope(tmp_path),
    )

    assert result.decision == "dry_run_only"
    assert result.reasons == []
    assert result.duplicate_idempotency_key is False
    assert result.target_writes_performed is False
    assert existing.read_text(encoding="utf-8") == "before\n"
    audit = read_audit(result.audit_artifact_path)
    assert audit["decision"] == "dry_run_only"
    assert audit["candidate"]["writes"] == [{"path": "src/allowed.txt", "operation": "write_text", "content_sha256": "0" * 64}]
    assert audit["safety_switches"]["allow_write"] is False
    assert audit["safety_switches"]["allow_secret_access"] is False


def test_expired_envelope_denies(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=make_candidate(target_root),
        approval_envelope=make_envelope(tmp_path, expires_delta=timedelta(minutes=-1)),
    )

    assert result.decision == "deny"
    assert "approval_envelope_expired" in result.reasons


def test_path_outside_approved_paths_denies(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    envelope = make_envelope(tmp_path)
    envelope["approved_paths"] = ["src/allowed.txt"]
    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=make_candidate(target_root, path="src/other.txt"),
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "path_not_approved:src/other.txt" in result.reasons
    assert not (target_root / "src" / "other.txt").exists()


def test_forbidden_path_denies_even_when_approved(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=make_candidate(target_root, path="src/forbidden/secret.txt"),
        approval_envelope=make_envelope(tmp_path),
    )

    assert result.decision == "deny"
    assert "forbidden_path:src/forbidden/secret.txt" in result.reasons
    assert not (target_root / "src" / "forbidden" / "secret.txt").exists()


def test_missing_rollback_plan_denies(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    envelope = make_envelope(tmp_path)
    Path(envelope["rollback_plan_path"]).unlink()
    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=make_candidate(target_root),
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "missing_rollback_plan" in result.reasons


def test_missing_verification_commands_denies(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    envelope = make_envelope(tmp_path)
    envelope["verification_commands"] = []
    result = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=make_candidate(target_root),
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "missing_verification_commands" in result.reasons


def test_duplicate_idempotency_key_is_detected(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    candidate = make_candidate(target_root)
    envelope = make_envelope(tmp_path, key="duplicate-key")

    first = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=candidate,
        approval_envelope=envelope,
    )
    second = evaluate_write_gate_dry_run(
        config=make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=candidate,
        approval_envelope=envelope,
    )

    assert first.decision == "dry_run_only"
    assert second.decision == "deny"
    assert second.duplicate_idempotency_key is True
    assert "duplicate_idempotency_key" in second.reasons
    ledger_path = tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d1-dry-run-write-gate" / "write-gate-ledger.sqlite"
    assert ledger_path.exists()
    assert not (ledger_path.parent / "idempotency-index.json").exists()
    with sqlite3.connect(ledger_path) as conn:
        rows = conn.execute(
            "SELECT idempotency_key FROM write_gate_idempotency WHERE idempotency_key = ?",
            ("duplicate-key",),
        ).fetchall()
    assert rows == [("duplicate-key",)]


def test_cli_write_gate_dry_run_outputs_decision_and_audit_artifact(tmp_path: Path) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    candidate_path = tmp_path / "candidate.json"
    envelope_path = tmp_path / "envelope.json"
    config_path = tmp_path / "aiwg.yaml"
    candidate_path.write_text(json.dumps(make_candidate(target_root), ensure_ascii=False), encoding="utf-8")
    envelope_path.write_text(json.dumps(make_envelope(tmp_path, key="cli-key"), ensure_ascii=False), encoding="utf-8")
    config_path.write_text(yaml.safe_dump(make_config(tmp_path), sort_keys=False), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "write-gate-dry-run",
            "--config",
            str(config_path),
            "--project-root",
            str(tmp_path),
            "--candidate",
            str(candidate_path),
            "--envelope",
            str(envelope_path),
            "--json",
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
    assert payload["target_writes_performed"] is False
    assert Path(payload["audit_artifact_path"]).exists()
    assert not (target_root / "src" / "allowed.txt").exists()

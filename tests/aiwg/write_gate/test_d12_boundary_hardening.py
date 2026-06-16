from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]


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


def make_config(orchestrator_root: Path, *, artifact_root: str | None = None) -> dict:
    return {
        "project_root": ".",
        "orchestrator_root": str(orchestrator_root),
        "artifact_root": artifact_root or "docs/ai-workgroup/state/artifacts",
        "policy": safety_policy(),
    }


def make_candidate(target_root: Path, *, path: str = "src/allowed.txt", key_suffix: str = "001") -> dict:
    return {
        "schema_version": "aiwg.phase_d1_candidate_write_intent.v1",
        "phase": "D1",
        "task_id": f"D1-task-{key_suffix}",
        "message_id": f"D1-msg-{key_suffix}",
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


def write_rollback_plan(orchestrator_root: Path, *, key_suffix: str = "001") -> Path:
    rollback_path = (
        orchestrator_root
        / "docs"
        / "ai-workgroup"
        / "state"
        / "artifacts"
        / "phase-d12-test"
        / f"rollback-{key_suffix}.json"
    )
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path.write_text(
        json.dumps(
            {
                "schema_version": "aiwg.phase_d1_rollback_plan.v1",
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


def make_envelope(orchestrator_root: Path, *, key_suffix: str = "001", rollback_path: Path | None = None) -> dict:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "schema_version": "aiwg.phase_d1_approval_envelope.v1",
        "phase": "D1",
        "task_id": f"D1-task-{key_suffix}",
        "message_id": f"D1-msg-{key_suffix}",
        "operator": "Human",
        "approved_paths": ["src/**"],
        "forbidden_paths": [],
        "rollback_plan_path": str(rollback_path or write_rollback_plan(orchestrator_root, key_suffix=key_suffix)),
        "verification_commands": ["python -m pytest -q tests/aiwg/write_gate"],
        "expires_at": expires_at.isoformat(),
        "idempotency_key": f"d12-key-{key_suffix}",
    }


def assert_no_business_artifacts(target_root: Path) -> None:
    forbidden = target_root / "docs" / "ai-workgroup" / "state" / "artifacts"
    assert not forbidden.exists(), f"business repository artifact tree was created: {forbidden}"
    assert not list(target_root.rglob("audit-D1-msg-*.json"))
    assert not list(target_root.rglob("idempotency-index.json"))


def test_explicit_target_root_is_separate_from_legacy_project_root(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    candidate = make_candidate(target_root)
    candidate.pop("target_root")

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=candidate,
        approval_envelope=make_envelope(orchestrator_root),
    )

    assert result.decision == "dry_run_only"
    safe_base = (orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve(strict=False)
    assert result.audit_artifact_path.resolve(strict=False).is_relative_to(safe_base)
    assert_no_business_artifacts(target_root)


def test_explicit_target_root_mismatch_denies_instead_of_silently_retargeting(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    other_target = tmp_path / "OtherTarget"
    orchestrator_root.mkdir()
    target_root.mkdir()
    other_target.mkdir()

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(other_target, key_suffix="target-mismatch"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="target-mismatch"),
    )

    assert result.decision == "deny"
    assert "candidate_target_root_mismatch" in result.reasons
    assert_no_business_artifacts(target_root)
    assert_no_business_artifacts(other_target)


def test_explicit_orchestrator_root_keeps_audit_and_idempotency_out_of_target_even_if_project_root_is_target(
    tmp_path: Path,
) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root),
        approval_envelope=make_envelope(orchestrator_root),
    )

    assert result.decision == "dry_run_only"
    safe_base = (orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve(strict=False)
    assert result.audit_artifact_path.resolve(strict=False).is_relative_to(safe_base)
    ledger = safe_base / "phase-d1-dry-run-write-gate" / "write-gate-ledger.sqlite"
    assert ledger.exists()
    assert not (ledger.parent / "idempotency-index.json").exists()
    assert_no_business_artifacts(target_root)


def test_rollback_guard_uses_orchestrator_root_not_project_root(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    rollback_path = write_rollback_plan(orchestrator_root, key_suffix="rollback-root")

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="rollback-root"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="rollback-root", rollback_path=rollback_path),
    )

    assert result.decision == "dry_run_only"
    assert "rollback_plan_outside_orchestrator_artifacts" not in result.reasons
    assert_no_business_artifacts(target_root)


@pytest.mark.parametrize("path", ["C:foo.txt", "src/name:stream.txt", "src/dir:ads/file.txt"])
def test_colon_write_paths_are_denied_even_if_pattern_approved(tmp_path: Path, path: str) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, path=path, key_suffix="colon"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="colon"),
    )

    assert result.decision == "deny"
    assert f"colon_write_path:{path}" in result.reasons
    assert_no_business_artifacts(target_root)


def test_cli_orchestrator_root_prevents_project_root_target_artifact_writes(tmp_path: Path) -> None:
    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    config_path = tmp_path / "aiwg.yaml"
    candidate_path = tmp_path / "candidate.json"
    envelope_path = tmp_path / "envelope.json"

    config_path.write_text(yaml.safe_dump(make_config(orchestrator_root), sort_keys=False), encoding="utf-8")
    candidate_path.write_text(json.dumps(make_candidate(target_root, key_suffix="cli"), ensure_ascii=False), encoding="utf-8")
    envelope_path.write_text(json.dumps(make_envelope(orchestrator_root, key_suffix="cli"), ensure_ascii=False), encoding="utf-8")

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
            "--orchestrator-root",
            str(orchestrator_root),
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
    assert Path(payload["audit_artifact_path"]).resolve(strict=False).is_relative_to(
        (orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve(strict=False)
    )
    assert_no_business_artifacts(target_root)


def test_cli_explicit_orchestrator_root_wins_when_config_lacks_orchestrator_root(tmp_path: Path) -> None:
    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    config_path = tmp_path / "aiwg.yaml"
    candidate_path = tmp_path / "candidate.json"
    envelope_path = tmp_path / "envelope.json"

    config = make_config(orchestrator_root)
    config.pop("orchestrator_root")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    candidate_path.write_text(json.dumps(make_candidate(target_root, key_suffix="cli-explicit"), ensure_ascii=False), encoding="utf-8")
    envelope_path.write_text(json.dumps(make_envelope(orchestrator_root, key_suffix="cli-explicit"), ensure_ascii=False), encoding="utf-8")

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
            "--orchestrator-root",
            str(orchestrator_root),
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
    assert Path(payload["audit_artifact_path"]).resolve(strict=False).is_relative_to(
        (orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve(strict=False)
    )
    assert_no_business_artifacts(target_root)


def test_cli_config_directory_defaults_to_orchestrator_root_when_project_root_is_target(tmp_path: Path) -> None:
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
    candidate_path.write_text(json.dumps(make_candidate(target_root, key_suffix="cli-config-dir"), ensure_ascii=False), encoding="utf-8")
    envelope_path.write_text(json.dumps(make_envelope(orchestrator_root, key_suffix="cli-config-dir"), ensure_ascii=False), encoding="utf-8")

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
    assert Path(payload["audit_artifact_path"]).resolve(strict=False).is_relative_to(
        (orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve(strict=False)
    )
    assert_no_business_artifacts(target_root)

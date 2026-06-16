from __future__ import annotations

import json
import os
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


def make_config(tmp_path: Path, *, artifact_root: str | None = None) -> dict:
    config = {
        "project_root": ".",
        "orchestrator_root": str(tmp_path),
        "artifact_root": artifact_root or "docs/ai-workgroup/state/artifacts",
        "policy": safety_policy(),
    }
    return config


def make_candidate(target_root: Path, **overrides: object) -> dict:
    candidate = {
        "schema_version": "aiwg.phase_d1_candidate_write_intent.v1",
        "phase": "D1",
        "task_id": "D1-task-001",
        "message_id": "D1-msg-001",
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
    candidate.update(overrides)
    return candidate


def write_rollback_plan(tmp_path: Path, **overrides: object) -> Path:
    rollback_path = tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d11-test" / "rollback.json"
    rollback_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "aiwg.phase_d1_rollback_plan.v1",
        "phase": "D1",
        "task_id": "D1-task-001",
        "message_id": "D1-msg-001",
        "target_writes_performed": False,
        "protected_business_repository_write_performed": False,
        "rollback_steps": ["restore from pre-write hashes"],
    }
    payload.update(overrides)
    rollback_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return rollback_path


def make_envelope(tmp_path: Path, **overrides: object) -> dict:
    rollback_path = write_rollback_plan(tmp_path)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    envelope = {
        "schema_version": "aiwg.phase_d1_approval_envelope.v1",
        "phase": "D1",
        "task_id": "D1-task-001",
        "message_id": "D1-msg-001",
        "operator": "Human",
        "approved_paths": ["src/**"],
        "forbidden_paths": ["src/forbidden/**"],
        "rollback_plan_path": str(rollback_path),
        "verification_commands": ["python -m pytest -q tests/aiwg/write_gate"],
        "expires_at": expires_at.isoformat(),
        "idempotency_key": "d11-key-001",
    }
    envelope.update(overrides)
    return envelope


def evaluate(tmp_path: Path, candidate: dict, envelope: dict, *, config: dict | None = None):
    from aiwg.write_gate import evaluate_write_gate_dry_run

    return evaluate_write_gate_dry_run(
        config=config or make_config(tmp_path),
        project_root=tmp_path,
        candidate_intent=candidate,
        approval_envelope=envelope,
    )


@pytest.mark.parametrize(
    ("candidate_overrides", "envelope_overrides", "expected_reason"),
    [
        ({"phase": "D2"}, {}, "candidate_envelope_phase_mismatch"),
        ({"task_id": "D1-task-B"}, {}, "candidate_envelope_task_id_mismatch"),
        ({"message_id": "D1-msg-B"}, {}, "candidate_envelope_message_id_mismatch"),
        ({}, {"phase": "D2"}, "unsupported_phase:D2"),
    ],
)
def test_candidate_and_envelope_identity_must_bind_before_dry_run(
    tmp_path: Path,
    candidate_overrides: dict,
    envelope_overrides: dict,
    expected_reason: str,
) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    result = evaluate(
        tmp_path,
        make_candidate(target_root, **candidate_overrides),
        make_envelope(tmp_path, **envelope_overrides),
    )

    assert result.decision == "deny"
    assert expected_reason in result.reasons
    assert result.target_writes_performed is False


def test_missing_candidate_phase_denies_even_when_schema_mentions_d1(tmp_path: Path) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    candidate = make_candidate(target_root)
    candidate.pop("phase")

    result = evaluate(tmp_path, candidate, make_envelope(tmp_path))

    assert result.decision == "deny"
    assert "missing_candidate_phase" in result.reasons


def test_artifact_root_outside_orchestrator_artifacts_denies_and_falls_back_to_safe_audit_dir(tmp_path: Path) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    unsafe_artifact_root = target_root / "docs" / "ai-workgroup" / "state" / "artifacts"

    result = evaluate(
        tmp_path,
        make_candidate(target_root),
        make_envelope(tmp_path),
        config=make_config(tmp_path, artifact_root=str(unsafe_artifact_root)),
    )

    assert result.decision == "deny"
    assert "artifact_root_outside_orchestrator_artifacts" in result.reasons
    assert result.audit_artifact_path.exists()
    safe_base = (tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts").resolve(strict=False)
    assert result.audit_artifact_path.resolve(strict=False).is_relative_to(safe_base)
    assert not unsafe_artifact_root.exists()


def test_rollback_plan_must_live_in_orchestrator_artifacts_and_match_schema(tmp_path: Path) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    unsafe_rollback = target_root / "rollback.json"
    unsafe_rollback.write_text(
        json.dumps(
            {
                "schema_version": "aiwg.phase_d1_rollback_plan.v1",
                "phase": "D1",
                "task_id": "D1-task-001",
                "message_id": "D1-msg-001",
                "target_writes_performed": False,
                "protected_business_repository_write_performed": False,
                "rollback_steps": ["restore"],
            }
        ),
        encoding="utf-8",
    )
    envelope = make_envelope(tmp_path, rollback_plan_path=str(unsafe_rollback))

    unsafe_result = evaluate(tmp_path, make_candidate(target_root), envelope)
    assert unsafe_result.decision == "deny"
    assert "rollback_plan_outside_orchestrator_artifacts" in unsafe_result.reasons

    malformed_rollback = tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d11-test-malformed" / "rollback.json"
    malformed_rollback.parent.mkdir(parents=True, exist_ok=True)
    malformed_rollback.write_text(
        json.dumps(
            {
                "schema_version": "aiwg.invalid.v1",
                "phase": "D1",
                "task_id": "D1-task-001",
                "message_id": "D1-msg-001",
                "target_writes_performed": False,
                "protected_business_repository_write_performed": False,
                "rollback_steps": ["restore"],
            }
        ),
        encoding="utf-8",
    )
    malformed_envelope = make_envelope(tmp_path, rollback_plan_path=str(malformed_rollback))
    malformed_result = evaluate(tmp_path, make_candidate(target_root), malformed_envelope)
    assert malformed_result.decision == "deny"
    assert "invalid_rollback_plan_schema" in malformed_result.reasons


@pytest.mark.parametrize(
    ("path", "expected_reason_prefix"),
    [
        ("D:/example/protected-business-repo/src/escape.py", "absolute_write_path:"),
        ("C:\\Windows\\System32\\drivers\\etc\\hosts", "absolute_write_path:"),
        ("//server/share/escape.py", "unc_write_path:"),
        ("src/../escape.py", "path_traversal:"),
        ("src/CON.txt", "reserved_windows_device_path:"),
    ],
)
def test_windows_canonical_path_guard_denies_unsafe_paths_even_when_approved_by_pattern(
    tmp_path: Path,
    path: str,
    expected_reason_prefix: str,
) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    candidate = make_candidate(
        target_root,
        writes=[
            {
                "path": path,
                "operation": "write_text",
                "content_sha256": "0" * 64,
                "content_preview": "[REDACTED]",
            }
        ],
    )
    envelope = make_envelope(tmp_path, approved_paths=["**"], forbidden_paths=[])

    result = evaluate(tmp_path, candidate, envelope)

    assert result.decision == "deny"
    assert any(reason.startswith(expected_reason_prefix) for reason in result.reasons)


def test_symlink_escape_from_target_root_denies_when_platform_supports_symlink(tmp_path: Path) -> None:
    target_root = tmp_path / "AIVideoTrans"
    outside = tmp_path / "outside"
    (target_root / "src").mkdir(parents=True)
    outside.mkdir()
    link = target_root / "src" / "link-out"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink not available on this platform: {exc}")

    candidate = make_candidate(target_root, writes=[{"path": "src/link-out/escape.txt", "operation": "write_text", "content_sha256": "0" * 64}])
    envelope = make_envelope(tmp_path, approved_paths=["src/**"], forbidden_paths=[])

    result = evaluate(tmp_path, candidate, envelope)

    assert result.decision == "deny"
    assert "candidate_path_escapes_target_root:src/link-out/escape.txt" in result.reasons
    assert not (outside / "escape.txt").exists()


def test_cli_fail_on_deny_returns_nonzero_but_still_writes_json_decision(tmp_path: Path) -> None:
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    candidate_path = tmp_path / "candidate.json"
    envelope_path = tmp_path / "envelope.json"
    config_path = tmp_path / "aiwg.yaml"
    candidate_path.write_text(json.dumps(make_candidate(target_root, task_id="other-task"), ensure_ascii=False), encoding="utf-8")
    envelope_path.write_text(json.dumps(make_envelope(tmp_path), ensure_ascii=False), encoding="utf-8")
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
            "--fail-on-deny",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 3, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["decision"] == "deny"
    assert "candidate_envelope_task_id_mismatch" in payload["reasons"]
    assert payload["target_writes_performed"] is False

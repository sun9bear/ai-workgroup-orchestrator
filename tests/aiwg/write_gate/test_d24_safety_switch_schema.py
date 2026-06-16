from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

APPROVAL_SCHEMA = "aiwg.phase_d1_approval_envelope.v1"
ROLLBACK_SCHEMA = "aiwg.phase_d1_rollback_plan.v1"
SAFETY_SWITCH_KEYS = (
    "allow_write",
    "allow_real_agents",
    "allow_real_adapter_dispatch",
    "allow_real_process_execution",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_modify_codex_automations",
    "allow_secret_access",
    "allow_network_write",
    "allow_destructive_commands",
)


def safety_policy(**overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "safe_mode": True,
        "allow_external_agents": False,
        **{key: False for key in SAFETY_SWITCH_KEYS},
    }
    policy.update(overrides)
    return policy


def make_config(orchestrator_root: Path, *, policy: Any | None = None) -> dict[str, Any]:
    return {
        "project_root": ".",
        "orchestrator_root": str(orchestrator_root),
        "artifact_root": "docs/ai-workgroup/state/artifacts",
        "policy": policy if policy is not None else safety_policy(),
    }


def artifact_base(orchestrator_root: Path) -> Path:
    return orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts"


def write_rollback_plan(orchestrator_root: Path, *, key_suffix: str) -> Path:
    rollback_path = artifact_base(orchestrator_root) / "phase-d24-test" / f"rollback-{key_suffix}.json"
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


def make_candidate(target_root: Path, *, key_suffix: str) -> dict[str, Any]:
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


def make_envelope(orchestrator_root: Path, *, key_suffix: str) -> dict[str, Any]:
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
        "idempotency_key": f"d24-key-{key_suffix}",
    }


def assert_no_business_artifacts(target_root: Path) -> None:
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts").exists()
    assert not list(target_root.rglob("write-gate-ledger.sqlite"))
    assert not list(target_root.rglob("idempotency-index.json"))
    assert not list(target_root.rglob("audit-D1-msg-*.json"))
    assert not list(target_root.rglob(".audit-*.pending"))
    assert not list(target_root.rglob(".audit-*.pending.tmp"))


def evaluate_with_policy(tmp_path: Path, policy: dict[str, Any], *, key_suffix: str):
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    result = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root, policy=policy),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix=key_suffix),
        approval_envelope=make_envelope(orchestrator_root, key_suffix=key_suffix),
    )
    audit = json.loads(result.audit_artifact_path.read_text(encoding="utf-8"))
    assert_no_business_artifacts(target_root)
    return result, audit


def test_allow_write_true_denies_even_for_valid_dry_run_envelope(tmp_path: Path) -> None:
    result, audit = evaluate_with_policy(tmp_path, safety_policy(allow_write=True), key_suffix="allow-write")

    assert result.decision == "deny"
    assert "unsafe_safety_switch_enabled:allow_write" in result.reasons
    assert result.target_writes_performed is False
    assert audit["decision"] == "deny"
    assert audit["safety_switches"] == {key: False for key in SAFETY_SWITCH_KEYS}


@pytest.mark.parametrize(
    ("switch_name", "switch_value"),
    [
        ("allow_write", "false"),
        ("allow_real_agents", 0),
        ("allow_push", 1),
        ("allow_secret_access", None),
    ],
)
def test_safety_switch_values_must_be_json_boolean_false(
    tmp_path: Path, switch_name: str, switch_value: Any
) -> None:
    result, audit = evaluate_with_policy(
        tmp_path,
        safety_policy(**{switch_name: switch_value}),
        key_suffix=f"bad-{switch_name}",
    )

    assert result.decision == "deny"
    assert f"invalid_safety_switch_type:{switch_name}" in result.reasons
    assert audit["decision"] == "deny"
    assert audit["safety_switches"] == {key: False for key in SAFETY_SWITCH_KEYS}


def test_policy_section_must_be_mapping_for_safety_switch_schema(tmp_path: Path) -> None:
    result, audit = evaluate_with_policy(tmp_path, [], key_suffix="bad-policy-shape")

    assert result.decision == "deny"
    assert "invalid_policy_shape" in result.reasons
    assert audit["decision"] == "deny"
    assert audit["safety_switches"] == {key: False for key in SAFETY_SWITCH_KEYS}


def test_explicit_null_policy_section_denies_instead_of_defaulting(tmp_path: Path) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    config = make_config(orchestrator_root)
    config["policy"] = None

    result = write_gate.evaluate_write_gate_dry_run(
        config=config,
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="null-policy"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="null-policy"),
    )
    audit = json.loads(result.audit_artifact_path.read_text(encoding="utf-8"))

    assert result.decision == "deny"
    assert "invalid_policy_shape" in result.reasons
    assert audit["decision"] == "deny"
    assert audit["safety_switches"] == {key: False for key in SAFETY_SWITCH_KEYS}
    assert_no_business_artifacts(target_root)

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def artifact_base(orchestrator_root: Path) -> Path:
    return orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts"


def artifact_dir(orchestrator_root: Path) -> Path:
    return artifact_base(orchestrator_root) / "phase-d1-dry-run-write-gate"


def ledger_path(orchestrator_root: Path) -> Path:
    return artifact_dir(orchestrator_root) / LEDGER_FILENAME


def write_rollback_plan(orchestrator_root: Path, *, key_suffix: str) -> Path:
    rollback_path = artifact_base(orchestrator_root) / "phase-d23-test" / f"rollback-{key_suffix}.json"
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


def make_candidate(target_root: Path, *, key_suffix: str) -> dict:
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


def make_envelope(orchestrator_root: Path, *, key_suffix: str) -> dict:
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
        "idempotency_key": f"d23-key-{key_suffix}",
    }


def pending_path_for_final(final_path: Path) -> Path:
    audit_id = final_path.stem.rsplit("-", 1)[-1]
    return final_path.parent / f".audit-{audit_id}.pending"


def fetch_evaluation_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT count(*) FROM write_gate_evaluations").fetchone()[0])


def assert_no_business_artifacts(target_root: Path) -> None:
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts").exists()
    assert not list(target_root.rglob("audit-D1-msg-*.json"))
    assert not list(target_root.rglob(".audit-*.pending"))
    assert not list(target_root.rglob(LEDGER_FILENAME))
    assert not list(target_root.rglob("idempotency-index.json"))


def create_interrupted_evaluation(
    *,
    write_gate,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_root: Path,
    target_root: Path,
    key_suffix: str,
) -> tuple[Path, Path]:
    monkeypatch.setattr(write_gate, "_finalize_staged_audit_artifact", lambda prepared: None)
    result = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix=key_suffix),
        approval_envelope=make_envelope(orchestrator_root, key_suffix=key_suffix),
    )
    final_path = result.audit_artifact_path
    pending_path = pending_path_for_final(final_path)
    assert not final_path.exists()
    assert pending_path.exists()
    monkeypatch.undo()
    return final_path, pending_path


def test_reconcile_does_not_finalize_tampered_pending_audit_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    interrupted_final, interrupted_pending = create_interrupted_evaluation(
        write_gate=write_gate,
        monkeypatch=monkeypatch,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        key_suffix="tampered",
    )
    payload = json.loads(interrupted_pending.read_text(encoding="utf-8"))
    payload["decision"] = "deny"
    payload["reasons"] = ["tampered_reason"]
    interrupted_pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    second = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="after-tamper"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="after-tamper"),
    )

    assert second.decision == "dry_run_only"
    assert not interrupted_final.exists()
    assert interrupted_pending.exists()
    assert fetch_evaluation_count(ledger_path(orchestrator_root)) == 2
    assert_no_business_artifacts(target_root)


def test_reconcile_does_not_finalize_malformed_pending_audit_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    interrupted_final, interrupted_pending = create_interrupted_evaluation(
        write_gate=write_gate,
        monkeypatch=monkeypatch,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        key_suffix="malformed",
    )
    interrupted_pending.write_text("{not-json", encoding="utf-8")

    second = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="after-malformed"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="after-malformed"),
    )

    assert second.decision == "dry_run_only"
    assert not interrupted_final.exists()
    assert interrupted_pending.exists()
    assert fetch_evaluation_count(ledger_path(orchestrator_root)) == 2
    assert_no_business_artifacts(target_root)


def test_reconcile_rejects_pending_audit_with_extra_raw_content_and_safety_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    interrupted_final, interrupted_pending = create_interrupted_evaluation(
        write_gate=write_gate,
        monkeypatch=monkeypatch,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        key_suffix="raw",
    )
    payload = json.loads(interrupted_pending.read_text(encoding="utf-8"))
    payload["raw_content"] = "this must never become a final audit artifact"
    payload["safety_switches"]["allow_write"] = True
    interrupted_pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="raw2"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="raw2"),
    )

    assert result.decision == "dry_run_only"
    assert not interrupted_final.exists()
    assert interrupted_pending.exists()
    assert_no_business_artifacts(target_root)


def test_reconcile_rejects_numeric_zero_safety_switches_as_type_loose_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    interrupted_final, interrupted_pending = create_interrupted_evaluation(
        write_gate=write_gate,
        monkeypatch=monkeypatch,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        key_suffix="zero",
    )
    payload = json.loads(interrupted_pending.read_text(encoding="utf-8"))
    payload["safety_switches"] = {key: 0 for key in payload["safety_switches"]}
    interrupted_pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="zero2"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="zero2"),
    )

    assert result.decision == "dry_run_only"
    assert not interrupted_final.exists()
    assert interrupted_pending.exists()
    assert_no_business_artifacts(target_root)


def test_reconcile_requires_duplicate_flag_to_be_boolean_and_ledger_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    first = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="dup"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="dup"),
    )
    assert first.decision == "dry_run_only"

    monkeypatch.setattr(write_gate, "_finalize_staged_audit_artifact", lambda prepared: None)
    duplicate = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="dup"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="dup"),
    )
    duplicate_final = duplicate.audit_artifact_path
    duplicate_pending = pending_path_for_final(duplicate_final)
    assert duplicate.decision == "deny"
    assert duplicate.duplicate_idempotency_key is True
    assert duplicate_pending.exists()
    monkeypatch.undo()

    payload = json.loads(duplicate_pending.read_text(encoding="utf-8"))
    payload["duplicate_idempotency_key"] = "truthy-string"
    duplicate_pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="dup2"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="dup2"),
    )

    assert result.decision == "dry_run_only"
    assert not duplicate_final.exists()
    assert duplicate_pending.exists()
    assert_no_business_artifacts(target_root)


def test_reconcile_finalizes_pending_audit_only_when_payload_matches_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    interrupted_final, interrupted_pending = create_interrupted_evaluation(
        write_gate=write_gate,
        monkeypatch=monkeypatch,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        key_suffix="valid-pending",
    )

    second = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="after-valid-pending"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="after-valid-pending"),
    )

    assert second.decision == "dry_run_only"
    assert interrupted_final.exists()
    assert not interrupted_pending.exists()
    assert json.loads(interrupted_final.read_text(encoding="utf-8"))["decision"] == "dry_run_only"
    assert fetch_evaluation_count(ledger_path(orchestrator_root)) == 2
    assert_no_business_artifacts(target_root)

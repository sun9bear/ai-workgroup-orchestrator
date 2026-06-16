from __future__ import annotations

import json
import os
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
    rollback_path = artifact_base(orchestrator_root) / "phase-d22-test" / f"rollback-{key_suffix}.json"
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
        "idempotency_key": f"d22-key-{key_suffix}",
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


def pending_path_for_final(final_path: Path) -> Path:
    audit_id = final_path.stem.rsplit("-", 1)[-1]
    return final_path.parent / f".audit-{audit_id}.pending"


@pytest.mark.parametrize("collision_shape", ["equal", "inside"])
def test_orchestrator_root_collision_with_target_root_fails_closed_before_any_artifact_write(
    tmp_path: Path,
    collision_shape: str,
) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    orchestrator_root = target_root if collision_shape == "equal" else target_root / "misconfigured-orchestrator"

    with pytest.raises(ValueError, match="orchestrator_root_collides_with_target_root"):
        evaluate_write_gate_dry_run(
            config=make_config(orchestrator_root),
            project_root=target_root,
            orchestrator_root=orchestrator_root,
            target_root=target_root,
            candidate_intent=make_candidate(target_root, key_suffix="root-collision"),
            approval_envelope=None,
        )

    if collision_shape == "inside":
        assert not orchestrator_root.exists()
    assert_no_business_artifacts(target_root)


def test_missing_final_audit_with_matching_pending_file_is_reconciled_on_next_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiwg.write_gate as write_gate

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()

    monkeypatch.setattr(write_gate, "_finalize_staged_audit_artifact", lambda prepared: None)
    first = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="interrupted"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="interrupted"),
    )
    interrupted_final = first.audit_artifact_path
    interrupted_pending = pending_path_for_final(interrupted_final)
    assert not interrupted_final.exists()
    assert interrupted_pending.exists()
    assert fetch_rows(ledger_path(orchestrator_root), "SELECT audit_artifact_path FROM write_gate_evaluations")

    monkeypatch.undo()
    second = write_gate.evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="reconciler"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="reconciler"),
    )

    assert second.decision == "dry_run_only"
    assert interrupted_final.exists()
    assert not interrupted_pending.exists()
    assert json.loads(interrupted_final.read_text(encoding="utf-8"))["decision"] == "dry_run_only"
    assert len(fetch_rows(ledger_path(orchestrator_root), "SELECT * FROM write_gate_evaluations")) == 2
    assert_no_business_artifacts(target_root)


def test_stale_unreferenced_pending_audit_file_is_cleaned_without_touching_fresh_pending(
    tmp_path: Path,
) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    gate_artifact_dir = artifact_dir(orchestrator_root)
    gate_artifact_dir.mkdir(parents=True)
    stale_pending = gate_artifact_dir / ".audit-stale.pending"
    stale_pending_tmp = gate_artifact_dir / ".audit-stale-tmp.pending.tmp"
    fresh_pending = gate_artifact_dir / ".audit-fresh.pending"
    stale_pending.write_text('{"status":"stale"}', encoding="utf-8")
    stale_pending_tmp.write_text('{"status":"stale-tmp"}', encoding="utf-8")
    fresh_pending.write_text('{"status":"fresh"}', encoding="utf-8")
    old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(stale_pending, (old_time, old_time))
    os.utime(stale_pending_tmp, (old_time, old_time))

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        target_root=target_root,
        candidate_intent=make_candidate(target_root, key_suffix="stale-cleanup"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="stale-cleanup"),
    )

    assert result.decision == "dry_run_only"
    assert not stale_pending.exists()
    assert not stale_pending_tmp.exists()
    assert fresh_pending.exists()
    assert_no_business_artifacts(target_root)

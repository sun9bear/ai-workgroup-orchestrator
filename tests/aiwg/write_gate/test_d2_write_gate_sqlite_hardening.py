from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def write_rollback_plan(orchestrator_root: Path, *, key_suffix: str = "001") -> Path:
    rollback_path = (
        orchestrator_root
        / "docs"
        / "ai-workgroup"
        / "state"
        / "artifacts"
        / "phase-d2-test"
        / f"rollback-{key_suffix}.json"
    )
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


def make_envelope(orchestrator_root: Path, *, key_suffix: str = "001", rollback_path: Path | str | None = None) -> dict:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "schema_version": APPROVAL_SCHEMA,
        "phase": "D1",
        "task_id": f"D1-task-{key_suffix}",
        "message_id": f"D1-msg-{key_suffix}",
        "operator": "Human",
        "approved_paths": ["src/**"],
        "forbidden_paths": [],
        "rollback_plan_path": str(rollback_path or write_rollback_plan(orchestrator_root, key_suffix=key_suffix)),
        "verification_commands": ["python -m pytest -q tests/aiwg/write_gate"],
        "expires_at": expires_at.isoformat(),
        "idempotency_key": f"d2-key-{key_suffix}",
    }


def artifact_base(orchestrator_root: Path) -> Path:
    return orchestrator_root / "docs" / "ai-workgroup" / "state" / "artifacts"


def ledger_path(orchestrator_root: Path) -> Path:
    return artifact_base(orchestrator_root) / "phase-d1-dry-run-write-gate" / LEDGER_FILENAME


def fetch_rows(db_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def assert_no_business_artifacts(target_root: Path) -> None:
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts").exists()
    assert not list(target_root.rglob("audit-D1-msg-*.json"))
    assert not list(target_root.rglob(LEDGER_FILENAME))
    assert not list(target_root.rglob("idempotency-index.json"))


def test_valid_dry_run_records_sqlite_ledger_idempotency_and_rollback_registry_without_json_index(
    tmp_path: Path,
) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    rollback_path = write_rollback_plan(orchestrator_root, key_suffix="ledger")

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="ledger"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="ledger", rollback_path=rollback_path),
    )

    assert result.decision == "dry_run_only"
    assert result.target_writes_performed is False
    db_path = ledger_path(orchestrator_root)
    assert db_path.exists()
    assert not (db_path.parent / "idempotency-index.json").exists()

    evaluation_rows = fetch_rows(db_path, "SELECT * FROM write_gate_evaluations")
    assert len(evaluation_rows) == 1
    assert evaluation_rows[0]["decision"] == "dry_run_only"
    assert evaluation_rows[0]["idempotency_key"] == "d2-key-ledger"
    assert evaluation_rows[0]["target_writes_performed"] == 0
    assert Path(evaluation_rows[0]["audit_artifact_path"]).resolve(strict=False) == result.audit_artifact_path.resolve(strict=False)

    idempotency_rows = fetch_rows(db_path, "SELECT * FROM write_gate_idempotency")
    assert len(idempotency_rows) == 1
    assert idempotency_rows[0]["idempotency_key"] == "d2-key-ledger"

    rollback_rows = fetch_rows(db_path, "SELECT * FROM write_gate_rollback_artifacts")
    assert len(rollback_rows) == 1
    assert Path(rollback_rows[0]["rollback_plan_path"]).resolve(strict=False) == rollback_path.resolve(strict=False)
    assert rollback_rows[0]["rollback_plan_sha256"] == hashlib.sha256(rollback_path.read_bytes()).hexdigest()
    assert rollback_rows[0]["target_writes_performed"] == 0

    table_info = fetch_rows(db_path, "PRAGMA table_info(write_gate_idempotency)")
    primary_key_columns = [row["name"] for row in table_info if row["pk"]]
    assert primary_key_columns == ["idempotency_key"]
    assert_no_business_artifacts(target_root)


def test_duplicate_idempotency_key_is_denied_by_sqlite_unique_row_and_first_record_is_preserved(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    candidate = make_candidate(target_root, key_suffix="dupe")
    envelope = make_envelope(orchestrator_root, key_suffix="dupe")

    first = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=candidate,
        approval_envelope=envelope,
    )
    second = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=candidate,
        approval_envelope=envelope,
    )

    assert first.decision == "dry_run_only"
    assert second.decision == "deny"
    assert second.duplicate_idempotency_key is True
    assert "duplicate_idempotency_key" in second.reasons

    db_path = ledger_path(orchestrator_root)
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_idempotency WHERE idempotency_key = ?", ("d2-key-dupe",))) == 1
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_evaluations ORDER BY created_at")) == 2
    assert_no_business_artifacts(target_root)


def test_invalid_approval_envelope_schema_denies_but_records_audit_evaluation_without_idempotency_insert(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    envelope = make_envelope(orchestrator_root, key_suffix="bad-schema")
    envelope["schema_version"] = "aiwg.invalid_envelope.v1"

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="bad-schema"),
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "invalid_approval_envelope_schema_version" in result.reasons
    db_path = ledger_path(orchestrator_root)
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_evaluations")) == 1
    assert fetch_rows(db_path, "SELECT * FROM write_gate_idempotency") == []
    assert_no_business_artifacts(target_root)


def test_approval_envelope_schema_requires_string_path_and_verification_lists(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    envelope = make_envelope(orchestrator_root, key_suffix="shape")
    envelope["approved_paths"] = "src/**"
    envelope["forbidden_paths"] = ["src/forbidden/**", 7]
    envelope["verification_commands"] = ["   "]

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="shape"),
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "invalid_approved_paths" in result.reasons
    assert "invalid_forbidden_paths" in result.reasons
    assert "invalid_verification_commands" in result.reasons
    db_path = ledger_path(orchestrator_root)
    assert fetch_rows(db_path, "SELECT * FROM write_gate_idempotency") == []
    assert_no_business_artifacts(target_root)


def test_malformed_scalar_envelope_lists_deny_without_exception_and_record_audit(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    envelope = make_envelope(orchestrator_root, key_suffix="scalar-shape")
    envelope["approved_paths"] = 7
    envelope["forbidden_paths"] = "src/forbidden/**"
    envelope["verification_commands"] = 3

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="scalar-shape"),
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "invalid_approved_paths" in result.reasons
    assert "invalid_forbidden_paths" in result.reasons
    assert "invalid_verification_commands" in result.reasons
    db_path = ledger_path(orchestrator_root)
    assert len(fetch_rows(db_path, "SELECT * FROM write_gate_evaluations")) == 1
    assert fetch_rows(db_path, "SELECT * FROM write_gate_idempotency") == []
    assert_no_business_artifacts(target_root)


def test_empty_candidate_write_path_denies_even_when_approval_pattern_is_broad(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    candidate = make_candidate(target_root, key_suffix="empty-path")
    candidate["writes"] = [{"path": "", "operation": "write_text", "content_sha256": "0" * 64}]
    envelope = make_envelope(orchestrator_root, key_suffix="empty-path")
    envelope["approved_paths"] = ["**"]

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=candidate,
        approval_envelope=envelope,
    )

    assert result.decision == "deny"
    assert "invalid_write_path:" in result.reasons
    assert fetch_rows(ledger_path(orchestrator_root), "SELECT * FROM write_gate_idempotency") == []
    assert_no_business_artifacts(target_root)


def test_relative_rollback_plan_path_resolves_under_orchestrator_root_and_is_registered(tmp_path: Path) -> None:
    from aiwg.write_gate import evaluate_write_gate_dry_run

    orchestrator_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    orchestrator_root.mkdir()
    target_root.mkdir()
    rollback_path = write_rollback_plan(orchestrator_root, key_suffix="relative")
    relative_rollback = rollback_path.relative_to(orchestrator_root).as_posix()

    result = evaluate_write_gate_dry_run(
        config=make_config(orchestrator_root),
        project_root=target_root,
        orchestrator_root=orchestrator_root,
        candidate_intent=make_candidate(target_root, key_suffix="relative"),
        approval_envelope=make_envelope(orchestrator_root, key_suffix="relative", rollback_path=relative_rollback),
    )

    assert result.decision == "dry_run_only"
    rollback_rows = fetch_rows(ledger_path(orchestrator_root), "SELECT rollback_plan_path FROM write_gate_rollback_artifacts")
    assert len(rollback_rows) == 1
    assert Path(rollback_rows[0]["rollback_plan_path"]).resolve(strict=False) == rollback_path.resolve(strict=False)
    assert_no_business_artifacts(target_root)

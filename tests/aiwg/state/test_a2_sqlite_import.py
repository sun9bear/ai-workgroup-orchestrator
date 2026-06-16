from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.protocol.frontmatter import parse_message_file
from aiwg.state import importer as importer_module
from aiwg.state.database import connect_database, init_database
from aiwg.state.importer import import_inbox, legacy_audit, list_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def write_config(tmp_path: Path, config: dict | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def write_message(
    project_root: Path,
    *,
    folder: str = "inbox",
    agent: str = "Fake",
    message_id: str = "A2-msg-001",
    task: str = "A2",
    to_agent: str = "Fake",
    status: str = "ready",
    can_write: str = "false",
    allowed_files: str = "[]",
) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / folder
        / agent
        / f"2026-06-04T120000_from-CodeX_to-{to_agent}_type-instruction_task-{task}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = [
        "---",
        f"id: {message_id}",
        f"task: {task}",
        "from: CodeX",
        f"to: {to_agent}",
        "type: instruction",
        f"status: {status}",
        "priority: medium",
        'reply_to: ""',
        "requires_human: false",
        "created_at: 2026-06-04T12:00:00+08:00",
        f"can_write: {can_write}",
        "context_files:",
        "  - docs/ai-workgroup/00-protocol.md",
    ]
    if allowed_files.startswith("\n"):
        frontmatter_lines.append("allowed_files:")
        frontmatter_lines.extend(allowed_files.strip("\n").splitlines())
    else:
        frontmatter_lines.append(f"allowed_files: {allowed_files}")
    frontmatter_lines.extend(
        [
            "forbidden_files:",
            "  - .env",
            "acceptance:",
            f"  - python -m aiwg.cli validate-message {path.as_posix()}",
            'claimed_by: ""',
            'claimed_at: ""',
            'lock_id: ""',
            "attempt: 0",
            "max_attempts: 2",
            "timeout_minutes: 30",
            "review_delegate: CodeX",
            "---",
            "",
            "# A2 fixture",
            "",
            "用于 Phase A2 import 测试。",
            "",
        ]
    )
    path.write_text("\n".join(frontmatter_lines), encoding="utf-8")
    return path


def test_init_database_creates_schema_migration_and_connection_pragmas(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)

    db_path = init_database(config=config, project_root=tmp_path)

    assert db_path == tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    assert db_path.exists()

    with connect_database(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        assert {
            "schema_migrations",
            "tasks",
            "events",
            "agent_runs",
            "verification_runs",
            "agent_capabilities",
            "git_refs",
            "operator_approvals",
        }.issubset(table_names)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert {"legacy_imported", "legacy_source_path", "git_branch", "worktree_path"}.issubset(
            task_columns
        )


def test_import_inbox_dry_run_does_not_write_tasks(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id="A2-msg-dry-run")

    result = import_inbox(config=config, project_root=tmp_path, agent="Fake", dry_run=True)

    assert result.scanned == 1
    assert result.valid == 1
    assert result.imported == 0
    assert result.dry_run is True
    assert list_tasks(config=config, project_root=tmp_path) == []


def test_import_inbox_writes_tasks_events_and_is_idempotent(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    message_path = write_message(tmp_path, message_id="A2-msg-import")

    result = import_inbox(config=config, project_root=tmp_path, agent="Fake", dry_run=False)

    assert result.scanned == 1
    assert result.valid == 1
    assert result.imported == 1
    assert result.skipped_existing == 0

    tasks = list_tasks(config=config, project_root=tmp_path)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["id"] == "A2-msg-import"
    assert task["task_id"] == "A2"
    assert task["from_agent"] == "CodeX"
    assert task["to_agent"] == "Fake"
    assert task["status"] == "ready"
    assert task["can_write"] is False
    assert task["allowed_files"] == []
    assert task["forbidden_files"] == [".env"]
    assert task["message_path"].endswith(message_path.name)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='task_imported'").fetchone()[0] == 1

    second = import_inbox(config=config, project_root=tmp_path, agent="Fake", dry_run=False)
    assert second.imported == 0
    assert second.skipped_existing == 1


def test_import_inbox_preserves_raw_byte_content_hash_for_crlf_messages(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    message_path = write_message(tmp_path, message_id="A2-msg-crlf-hash")
    crlf_bytes = message_path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
    message_path.write_bytes(crlf_bytes)
    expected_hash = hashlib.sha256(crlf_bytes).hexdigest()

    result = import_inbox(config=config, project_root=tmp_path, agent="Fake", dry_run=False)

    assert result.imported == 1
    with connect_database(db_path) as conn:
        content_hash = conn.execute(
            "SELECT content_hash FROM tasks WHERE id = ?",
            ("A2-msg-crlf-hash",),
        ).fetchone()[0]
    assert content_hash == expected_hash


def write_import_manifest(path: Path, *message_paths: Path) -> Path:
    manifest = {
        "schema_version": "aiwg.test_import_manifest.v1",
        "import_policy": {
            "intended_import_status": "done",
            "can_write": False,
            "dispatchable": False,
        },
        "selected_candidates": [
            {
                "order": index,
                "absolute_path": message_path.as_posix(),
                "frontmatter_id": parse_message_file(message_path).frontmatter["id"],
                "import_decision": "evidence_only",
                "hashes": {"content_sha256": hashlib.sha256(message_path.read_bytes()).hexdigest()},
            }
            for index, message_path in enumerate(message_paths, start=1)
        ],
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_import_approval(
    path: Path,
    *,
    manifest_path: Path,
    project_root: Path,
    manifest_sha256: str | None = None,
    expires_at: str | None = None,
    decision: str = "approved",
    evidence_only: bool = True,
) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = {
        "schema_version": "aiwg.import_approval.v1",
        "approval_id": "approval-test-manifest-import",
        "decision": decision,
        "operator": "pytest",
        "approved_at": "2026-06-06T09:45:00+08:00",
        "expires_at": expires_at
        or (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds"),
        "import_mode": "manifest_evidence_only",
        "project_root": project_root.as_posix(),
        "manifest_path": manifest_path.as_posix(),
        "manifest_sha256": manifest_sha256 or hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "evidence_only": evidence_only,
        "selected_count": len(manifest["selected_candidates"]),
        "content_hash_algorithm": "aiwg.content_hash.raw_file_bytes_sha256.v1",
        "frontmatter_hash_algorithm": "aiwg.frontmatter_hash.normalized_json_sha256.v1",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_import_inbox_manifest_requires_approval_artifact_for_real_import(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    init_database(config=config, project_root=tmp_path)
    selected = write_message(tmp_path, message_id="P9-selected-needs-approval", status="ready")
    manifest_path = write_import_manifest(tmp_path / "p9-manifest.json", selected)

    with pytest.raises(ValueError, match="approval_artifact_required"):
        import_inbox(
            config=config,
            project_root=tmp_path,
            manifest_path=manifest_path,
            evidence_only=True,
        )


def test_import_inbox_manifest_rejects_approval_artifact_manifest_hash_mismatch(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    init_database(config=config, project_root=tmp_path)
    selected = write_message(tmp_path, message_id="P9-selected-bad-approval", status="ready")
    manifest_path = write_import_manifest(tmp_path / "p9-bad-manifest.json", selected)
    approval_path = write_import_approval(
        tmp_path / "p9-bad-approval.json",
        manifest_path=manifest_path,
        project_root=tmp_path,
        manifest_sha256="0" * 64,
    )

    with pytest.raises(ValueError, match="approval_manifest_sha256_mismatch"):
        import_inbox(
            config=config,
            project_root=tmp_path,
            manifest_path=manifest_path,
            evidence_only=True,
            approval_artifact_path=approval_path,
        )


def test_import_inbox_manifest_evidence_only_imports_only_selected_non_dispatchable_rows(
    tmp_path: Path,
) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    selected = write_message(
        tmp_path,
        message_id="P7-selected-ready-write",
        task="P7-selected",
        status="ready",
        can_write="true",
        allowed_files="\n  - docs/**",
    )
    unselected = write_message(
        tmp_path,
        agent="CodeX",
        message_id="P7-unselected-ready",
        task="P7-unselected",
        to_agent="CodeX",
        status="ready",
    )
    manifest_path = write_import_manifest(tmp_path / "p7-manifest.json", selected)
    approval_path = write_import_approval(
        tmp_path / "p7-approval.json",
        manifest_path=manifest_path,
        project_root=tmp_path,
    )

    result = import_inbox(
        config=config,
        project_root=tmp_path,
        manifest_path=manifest_path,
        evidence_only=True,
        approval_artifact_path=approval_path,
    )

    assert result.scanned == 1
    assert result.valid == 1
    assert result.invalid == 0
    assert result.imported == 1
    assert result.skipped_existing == 0
    assert result.manifest_path == manifest_path
    assert result.evidence_only is True

    tasks = list_tasks(config=config, project_root=tmp_path)
    assert [task["id"] for task in tasks] == ["P7-selected-ready-write"]
    task = tasks[0]
    assert task["status"] == "done"
    assert task["requires_human"] is True
    assert task["can_write"] is False
    assert task["allowed_files"] == []
    assert unselected.name not in task["message_path"]

    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT status, requires_human, can_write, worktree_required,
                   allowed_files_json, legacy_imported, legacy_source_path,
                   content_hash, frontmatter_hash
            FROM tasks
            WHERE id = ?
            """,
            ("P7-selected-ready-write",),
        ).fetchone()
        assert dict(row) == {
            "status": "done",
            "requires_human": 1,
            "can_write": 0,
            "worktree_required": 0,
            "allowed_files_json": "[]",
            "legacy_imported": 0,
            "legacy_source_path": selected.as_posix(),
            "content_hash": hashlib.sha256(selected.read_bytes()).hexdigest(),
            "frontmatter_hash": importer_module.compute_frontmatter_hash(parse_message_file(selected).frontmatter),
        }
        assert (
            conn.execute(
                "SELECT count(*) FROM tasks WHERE status='ready' AND requires_human=0 AND attempt < max_attempts"
            ).fetchone()[0]
            == 0
        )
        event_payload = conn.execute(
            "SELECT payload_json FROM events WHERE message_id = ? AND type='task_imported'",
            ("P7-selected-ready-write",),
        ).fetchone()[0]
        payload = json.loads(event_payload)
        assert payload["source"] == "import-inbox"
        assert payload["manifest_path"] == manifest_path.as_posix()
        assert payload["approval_artifact_path"] == approval_path.as_posix()
        assert len(payload["approval_artifact_sha256"]) == 64
        assert payload["content_hash_algorithm"] == "aiwg.content_hash.raw_file_bytes_sha256.v1"
        assert payload["frontmatter_hash_algorithm"] == "aiwg.frontmatter_hash.normalized_json_sha256.v1"
        assert payload["evidence_only"] is True
        assert payload["original_frontmatter"]["status"] == "ready"
        assert payload["original_frontmatter"]["can_write"] is True

    second = import_inbox(
        config=config,
        project_root=tmp_path,
        manifest_path=manifest_path,
        evidence_only=True,
        approval_artifact_path=approval_path,
    )
    assert second.imported == 0
    assert second.skipped_existing == 1


def test_cli_import_inbox_manifest_evidence_only_uses_explicit_opt_in(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    selected = write_message(tmp_path, message_id="P7-cli-selected", task="P7-cli", status="ready")
    write_message(
        tmp_path,
        agent="CodeX",
        message_id="P7-cli-unselected",
        task="P7-cli-unselected",
        to_agent="CodeX",
        status="ready",
    )
    manifest_path = write_import_manifest(tmp_path / "p7-cli-manifest.json", selected)
    approval_path = write_import_approval(
        tmp_path / "p7-cli-approval.json",
        manifest_path=manifest_path,
        project_root=tmp_path,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "import-inbox",
            "--config",
            str(config_path),
            "--manifest",
            str(manifest_path),
            "--evidence-only",
            "--approval-artifact",
            str(approval_path),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert "scanned=1" in completed.stdout
    assert "imported=1" in completed.stdout
    assert "manifest=" in completed.stdout
    assert "evidence_only=True" in completed.stdout

    with connect_database(tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, status, requires_human, can_write FROM tasks").fetchall()
        assert [dict(row) for row in rows] == [
            {"id": "P7-cli-selected", "status": "done", "requires_human": 1, "can_write": 0}
        ]


def test_legacy_audit_writes_report_without_importing_tasks(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    write_message(tmp_path, folder="done", agent="Fake", message_id="A2-msg-done", status="done")
    write_message(
        tmp_path,
        folder="inbox",
        agent="Fake",
        message_id="A2-msg-invalid",
        can_write="false",
        allowed_files="\n  - docs/**",
    )

    result = legacy_audit(config=config, project_root=tmp_path)

    assert result.mode == "audit_only"
    assert result.scanned == 2
    assert result.valid == 1
    assert result.invalid == 1
    assert result.imported == 0
    assert result.report_path is not None
    report = result.report_path.read_text(encoding="utf-8")
    assert "# Legacy Migration Audit Report" in report
    assert "mode: audit_only" in report
    assert "A2-msg-invalid" in report
    assert "Field 'allowed_files' must be empty when can_write is false" in report
    assert not (tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite").exists()


def test_cli_a2_commands_use_configured_project_root(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    write_message(tmp_path, message_id="A2-msg-cli")

    init_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "init-db", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    dry_run_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "import-inbox",
            "--config",
            str(config_path),
            "--agent",
            "Fake",
            "--dry-run",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    import_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "import-inbox",
            "--config",
            str(config_path),
            "--agent",
            "Fake",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    list_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "list-tasks", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    audit_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "legacy-audit", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert init_completed.returncode == 0, init_completed.stderr
    assert "Initialized SQLite database" in init_completed.stdout
    assert dry_run_completed.returncode == 0, dry_run_completed.stderr
    assert "dry_run=True" in dry_run_completed.stdout
    assert "imported=0" in dry_run_completed.stdout
    assert import_completed.returncode == 0, import_completed.stderr
    assert "imported=1" in import_completed.stdout
    assert list_completed.returncode == 0, list_completed.stderr
    assert "A2-msg-cli" in list_completed.stdout
    assert "ready" in list_completed.stdout
    assert audit_completed.returncode == 0, audit_completed.stderr
    assert "legacy audit" in audit_completed.stdout.lower()

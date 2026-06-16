from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.adapter_registry import write_restricted_adapter_preflight
from aiwg.config import build_default_config
from aiwg.runners.fake import FakeAdapter
from aiwg.state.database import init_database
from aiwg.state.importer import import_inbox, legacy_audit, list_tasks
from aiwg.verification import run_verification_commands
import aiwg.verification as verification_module


def build_boundary_config(project_root: Path, target_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    # D5.3.1 RED contract: generic artifact/evidence writers must treat these
    # roots as protected business repositories and fail closed before mkdir/write.
    config["protected_target_roots"] = [str(target_root)]
    return config


def task_fixture(*, message_id: str = "D531-msg", acceptance: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": message_id,
        "task_id": message_id,
        "message_path": f"docs/ai-workgroup/inbox/Fake/{message_id}.md",
        "from_agent": "CodeX",
        "to_agent": "Fake",
        "type": "instruction",
        "can_write": False,
        "requires_human": False,
        "allowed_files": [],
        "forbidden_files": [".env"],
        "context_files": [],
        "acceptance": acceptance or [],
        "attempt": 0,
        "max_attempts": 2,
        "timeout_minutes": 1,
    }


def yaml_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_verification_message(project_root: Path, *, message_id: str, acceptance: list[str]) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "Fake"
        / f"2026-06-07T000000_from-CodeX_to-Fake_type-instruction_task-{message_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {message_id}",
        f"task: {message_id}",
        "from: CodeX",
        "to: Fake",
        "type: instruction",
        "status: ready",
        "priority: medium",
        'reply_to: ""',
        "requires_human: false",
        "created_at: 2026-06-07T00:00:00+08:00",
        "can_write: false",
        "context_files: []",
        "allowed_files: []",
        "forbidden_files:",
        "  - .env",
        "acceptance:",
        *(f"  - {yaml_single_quoted(command)}" for command in acceptance),
        'claimed_by: ""',
        'claimed_at: ""',
        'lock_id: ""',
        "attempt: 0",
        "max_attempts: 2",
        "timeout_minutes: 1",
        "review_delegate: CodeX",
        "---",
        "",
        "# D5.3.1 verification boundary fixture",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_target_sentinel(target_root: Path) -> tuple[tuple[str, int], ...]:
    target_root.mkdir(parents=True, exist_ok=True)
    (target_root / "README.md").write_text("protected business repo sentinel\n", encoding="utf-8")
    return tree_digest(target_root)


def tree_digest(root: Path) -> tuple[tuple[str, int], ...]:
    if not root.exists():
        return ()
    return tuple(
        sorted(
            (path.relative_to(root).as_posix(), path.stat().st_size)
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def assert_target_unchanged(target_root: Path, before: tuple[tuple[str, int], ...]) -> None:
    assert tree_digest(target_root) == before


def test_adapter_registry_preflight_rejects_artifact_root_under_target_before_writing(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    before = write_target_sentinel(target_root)
    config = build_boundary_config(project_root, target_root)
    config["artifact_root"] = str(target_root / "aiwg-artifacts")

    with pytest.raises(ValueError, match="artifact_root_.*target|target_root|artifact_root_"):
        write_restricted_adapter_preflight(
            config=config,
            project_root=project_root,
            agent="OpenCode",
            adapter_type="opencode",
            task=task_fixture(message_id="D531-adapter-registry"),
        )

    assert_target_unchanged(target_root, before)


def test_fake_adapter_rejects_artifact_root_under_target_before_writing(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    before = write_target_sentinel(target_root)
    config = build_boundary_config(project_root, target_root)
    config["artifact_root"] = str(target_root / "fake-artifacts")

    with pytest.raises(ValueError, match="artifact_root_.*target|target_root|artifact_root_"):
        FakeAdapter().run(
            task=task_fixture(message_id="D531-fake-adapter"),
            config=config,
            project_root=project_root,
        )

    assert_target_unchanged(target_root, before)


def test_verification_rejects_artifact_root_under_target_before_any_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    before = write_target_sentinel(target_root)
    config = build_boundary_config(project_root, target_root)
    config["artifact_root"] = str(target_root / "verification-artifacts")
    init_database(config=config, project_root=project_root)
    write_verification_message(
        project_root,
        message_id="D531-verification",
        acceptance=["python -c 'print(should-not-run)'"],
    )
    imported = import_inbox(config=config, project_root=project_root, agent="Fake", dry_run=False)
    assert imported.imported == 1
    task = list_tasks(config=config, project_root=project_root, agent="Fake")[0]

    calls: list[str] = []

    def fake_subprocess_run(command: str, **_: Any) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="mocked verification\n", stderr="")

    monkeypatch.setattr(verification_module.subprocess, "run", fake_subprocess_run)

    with pytest.raises(ValueError, match="artifact_root_.*target|target_root|artifact_root_"):
        run_verification_commands(
            config=config,
            project_root=project_root,
            task=task,
            agent="Fake",
        )

    assert calls == []
    assert_target_unchanged(target_root, before)


def test_adapter_binary_readiness_rejects_report_path_under_target_before_writing(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    before = write_target_sentinel(target_root)
    config = build_boundary_config(project_root, target_root)
    config["artifact_root"] = str(target_root / "readiness-artifacts")

    with pytest.raises(ValueError, match="artifact_root_.*target|target_root|artifact_root_"):
        write_adapter_binary_readiness_report(
            config=config,
            project_root=project_root,
            run_version_probes=False,
        )

    assert_target_unchanged(target_root, before)


def test_legacy_audit_rejects_absolute_report_path_under_target_before_writing(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    before = write_target_sentinel(target_root)
    config = build_boundary_config(project_root, target_root)
    config["legacy_migration"]["report_path"] = str(target_root / "legacy-migration-report.md")

    with pytest.raises(ValueError, match="legacy|evidence|report|target_root|target"):
        legacy_audit(config=config, project_root=project_root)

    assert_target_unchanged(target_root, before)

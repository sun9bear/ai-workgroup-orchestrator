from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg import git_steward
from aiwg.config import build_default_config, dump_config
from aiwg.git_steward import get_pr_gate_status, plan_git_dry_run
from aiwg.state.database import connect_database, init_database, resolve_db_path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    return config


def write_config(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def assert_no_target_git_side_effects(target_root: Path) -> None:
    assert not (target_root / ".codex_worktrees").exists()
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d4-git-steward").exists()
    assert not list(target_root.rglob("git-plan-*.json"))
    assert not list(target_root.rglob("pr-gate-*.json"))


def test_d4_git_steward_schema_migration_is_installed(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)

    db_path = init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
        ]
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        assert {
            "git_worktree_proposals",
            "git_commit_proposals",
            "pr_gate_status",
        }.issubset(table_names)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_git_steward_dry_run_records_worktree_commit_and_pr_gate_without_target_writes(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    result = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D4-plan-apf-frontend",
        task_id="APF3b-frontend",
        target_root=target_root,
        requested_scope="apf_frontend",
        changed_files=[
            "frontend-next/src/components/marketing/anonymous-trial-launcher.tsx",
            ".codegraph/cache.json",
            ".codex_worktrees/tmp/generated.patch",
            "docs/ai-workgroup/state/tasks.sqlite",
        ],
        base_branch="main",
    )

    assert result.status == "planned"
    assert result.plan_id == "D4-plan-apf-frontend"
    assert result.dry_run is True
    assert result.branch_name is not None and result.branch_name.startswith("aiwg/apf-frontend/")
    assert result.worktree_path is not None and ".codex_worktrees" in result.worktree_path
    assert result.included_files == ["frontend-next/src/components/marketing/anonymous-trial-launcher.tsx"]
    assert {item["path"] for item in result.excluded_files} == {
        ".codegraph/cache.json",
        ".codex_worktrees/tmp/generated.patch",
        "docs/ai-workgroup/state/tasks.sqlite",
    }
    assert result.target_writes_performed is False
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert result.artifact_path is not None
    assert result.artifact_path.exists()
    assert result.artifact_path.is_relative_to(
        tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d4-git-steward"
    )
    artifact = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "aiwg.git_steward_dry_run.v1"
    assert artifact["target_writes_performed"] is False
    assert artifact["git_push_performed"] is False
    assert artifact["git_merge_performed"] is False
    assert artifact["mcp_mutation_tools_exposed"] is False
    assert_no_target_git_side_effects(target_root)

    db_path = resolve_db_path(config, tmp_path)
    assert db_rows(
        db_path,
        """
        SELECT plan_id, status, dry_run, target_writes_performed,
               git_push_performed, git_merge_performed, mcp_mutation_tools_exposed
        FROM git_worktree_proposals
        """,
    ) == [("D4-plan-apf-frontend", "planned", 1, 0, 0, 0, 0)]
    commit_rows = db_rows(
        db_path,
        """
        SELECT plan_id, status, scope_key, commit_performed, included_files_json
        FROM git_commit_proposals
        """,
    )
    assert len(commit_rows) == 1
    assert commit_rows[0][:4] == ("D4-plan-apf-frontend", "commit_proposed", "apf_frontend", 0)
    assert json.loads(commit_rows[0][4]) == ["frontend-next/src/components/marketing/anonymous-trial-launcher.tsx"]
    assert db_rows(
        db_path,
        """
        SELECT plan_id, gate_state, required_checks_state, review_threads_state,
               pr_mutation_performed, merge_performed
        FROM pr_gate_status
        """,
    ) == [("D4-plan-apf-frontend", "pr_not_created_dry_run", "not_polled", "not_polled", 0, 0)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT type FROM events WHERE task_id = ? ORDER BY id", ("D4-plan-apf-frontend",)) == [
        ("git_steward_plan_started",),
        ("git_steward_plan_written",),
    ]


def test_apf_frontend_and_backend_scopes_cannot_mix_into_one_commit_proposal(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    result = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D4-plan-mixed-scope",
        task_id="APF3b-mixed",
        target_root=target_root,
        requested_scope="apf_frontend",
        changed_files=[
            "frontend-next/src/components/marketing/anonymous-trial-launcher.tsx",
            "src/services/anonymous_preview_admission.py",
        ],
        base_branch="main",
    )

    assert result.status == "scope_mixed_denied"
    assert result.denied_reasons == ["mixed_scope_files: apf_backend, apf_frontend"]
    assert result.included_files == []
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert_no_target_git_side_effects(target_root)

    db_path = resolve_db_path(config, tmp_path)
    assert db_rows(db_path, "SELECT COUNT(*) FROM git_worktree_proposals") == [(0,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM git_commit_proposals") == [(0,)]
    assert db_rows(
        db_path,
        "SELECT plan_id, gate_state, merge_performed FROM pr_gate_status",
    ) == [("D4-plan-mixed-scope", "scope_mixed_denied", 0)]


def test_git_steward_denies_mutation_switches_even_in_dry_run(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["allow_push"] = True
    config["policy"]["allow_merge"] = True
    config["git"]["allow_auto_commit"] = True
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    result = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D4-plan-mutation-switches",
        task_id="APF3b-backend",
        target_root=target_root,
        requested_scope="apf_backend",
        changed_files=["src/services/anonymous_preview_admission.py"],
        base_branch="main",
    )

    assert result.status == "policy_denied"
    assert result.denied_reasons == [
        "allow_push=true",
        "allow_merge=true",
        "git.allow_auto_commit=true",
    ]
    assert result.target_writes_performed is False
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert_no_target_git_side_effects(target_root)

    db_path = resolve_db_path(config, tmp_path)
    assert db_rows(db_path, "SELECT COUNT(*) FROM git_worktree_proposals") == [(0,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM git_commit_proposals") == [(0,)]
    assert db_rows(db_path, "SELECT plan_id, gate_state FROM pr_gate_status") == [
        ("D4-plan-mutation-switches", "policy_denied")
    ]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "C:drive-relative.py",
        "D:/absolute.py",
        "../escape.py",
        "frontend-next/src/bad:name.tsx",
        ".",
        "./",
        "frontend-next/src/bad\x1fname.tsx",
        "frontend-next/src/trailing-newline.tsx\n",
        "\tfrontend-next/src/leading-tab.tsx",
        "frontend-next/src/trailing-unit-separator.tsx\x1f",
    ],
)
def test_git_steward_rejects_unsafe_changed_paths_before_writing(tmp_path: Path, unsafe_path: str) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    with pytest.raises(ValueError, match="unsafe_changed_path"):
        plan_git_dry_run(
            config=config,
            project_root=tmp_path,
            plan_id="D4-plan-unsafe-path",
            task_id="APF3b-unsafe",
            target_root=target_root,
            requested_scope="apf_frontend",
            changed_files=[unsafe_path],
            base_branch="main",
        )

    assert_no_target_git_side_effects(target_root)
    assert not resolve_db_path(config, tmp_path).exists()


def test_git_steward_does_not_leave_final_artifact_when_sqlite_transaction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    def fail_insert_event(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("forced sqlite failure")

    monkeypatch.setattr(git_steward, "_insert_event", fail_insert_event)

    with pytest.raises(sqlite3.OperationalError, match="forced sqlite failure"):
        plan_git_dry_run(
            config=config,
            project_root=tmp_path,
            plan_id="D4-plan-sqlite-fails",
            task_id="APF3b-frontend",
            target_root=target_root,
            requested_scope="apf_frontend",
            changed_files=["frontend-next/src/components/marketing/anonymous-trial-launcher.tsx"],
            base_branch="main",
        )

    phase_artifact_root = tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d4-git-steward"
    assert not (phase_artifact_root / "git-plan-d4-plan-sqlite-fails.json").exists()
    assert not list(phase_artifact_root.glob("git-plan-*.json"))
    assert_no_target_git_side_effects(target_root)


def test_git_steward_rejects_reused_plan_id_with_conflicting_identity(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    target_root = tmp_path / "AIVideoTrans"
    other_target_root = tmp_path / "OtherTarget"
    target_root.mkdir()
    other_target_root.mkdir()

    first = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D4-plan-reused-id",
        task_id="APF3b-frontend",
        target_root=target_root,
        requested_scope="apf_frontend",
        changed_files=["frontend-next/src/components/marketing/anonymous-trial-launcher.tsx"],
        base_branch="main",
    )
    assert first.status == "planned"
    assert first.artifact_path is not None
    first_artifact = json.loads(first.artifact_path.read_text(encoding="utf-8"))

    second = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D4-plan-reused-id",
        task_id="APF3b-backend",
        target_root=other_target_root,
        requested_scope="apf_backend",
        changed_files=["src/services/anonymous_preview_admission.py"],
        base_branch="main",
    )

    assert second.status == "plan_id_conflict_denied"
    assert second.denied_reasons == [
        "plan_id_conflict: existing proposal identity differs from requested identity"
    ]
    assert second.artifact_path is None
    assert json.loads(first.artifact_path.read_text(encoding="utf-8")) == first_artifact

    db_path = resolve_db_path(config, tmp_path)
    assert db_rows(
        db_path,
        "SELECT plan_id, task_id, target_root, scope_key FROM git_worktree_proposals",
    ) == [("D4-plan-reused-id", "APF3b-frontend", str(target_root.resolve()), "apf_frontend")]
    assert db_rows(
        db_path,
        "SELECT plan_id, task_id, scope_key, included_files_json FROM git_commit_proposals",
    ) == [
        (
            "D4-plan-reused-id",
            "APF3b-frontend",
            "apf_frontend",
            json.dumps(
                ["frontend-next/src/components/marketing/anonymous-trial-launcher.tsx"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    ]
    assert db_rows(
        db_path,
        "SELECT plan_id, gate_state FROM pr_gate_status",
    ) == [("D4-plan-reused-id", "pr_not_created_dry_run")]
    assert_no_target_git_side_effects(target_root)
    assert_no_target_git_side_effects(other_target_root)


def test_cli_git_plan_requires_dry_run_and_pr_gate_status_is_read_only(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config_path = write_config(tmp_path, config)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    missing_dry_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "git-plan",
            "--config",
            str(config_path),
            "--plan-id",
            "D4-cli-plan",
            "--task-id",
            "APF3b-cli",
            "--target-root",
            str(target_root),
            "--scope",
            "apf_frontend",
            "--changed-file",
            "frontend-next/src/components/marketing/anonymous-trial-launcher.tsx",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert missing_dry_run.returncode == 2
    assert "--dry-run is required" in missing_dry_run.stdout

    planned = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "git-plan",
            "--config",
            str(config_path),
            "--plan-id",
            "D4-cli-plan",
            "--task-id",
            "APF3b-cli",
            "--target-root",
            str(target_root),
            "--scope",
            "apf_frontend",
            "--changed-file",
            "frontend-next/src/components/marketing/anonymous-trial-launcher.tsx",
            "--dry-run",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert planned.returncode == 0, planned.stderr + planned.stdout
    payload = json.loads(planned.stdout)
    assert payload["status"] == "planned"
    assert payload["target_writes_performed"] is False
    assert payload["git_push_performed"] is False
    assert payload["git_merge_performed"] is False
    assert payload["mcp_mutation_tools_exposed"] is False
    assert_no_target_git_side_effects(target_root)

    status = get_pr_gate_status(config=config, project_root=tmp_path, plan_id="D4-cli-plan")
    assert status.plan_id == "D4-cli-plan"
    assert status.gate_state == "pr_not_created_dry_run"
    status_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "pr-gate-status",
            "--config",
            str(config_path),
            "--plan-id",
            "D4-cli-plan",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert status_cli.returncode == 0, status_cli.stderr + status_cli.stdout
    status_payload = json.loads(status_cli.stdout)
    assert status_payload["plan_id"] == "D4-cli-plan"
    assert status_payload["gate_state"] == "pr_not_created_dry_run"
    assert status_payload["read_only"] is True
    assert status_payload["mutation_actions"] == []

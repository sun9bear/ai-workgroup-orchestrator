from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.git_steward import plan_git_dry_run

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    return config


def assert_no_target_git_side_effects(target_root: Path) -> None:
    assert not (target_root / ".codex_worktrees").exists()
    assert not (target_root / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-d4-git-steward").exists()
    assert not list(target_root.rglob("git-plan-*.json"))
    assert not list(target_root.rglob("pr-gate-*.json"))


def run_plan(config: dict[str, Any], tmp_path: Path):
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    result = plan_git_dry_run(
        config=config,
        project_root=tmp_path,
        plan_id="D538-plan-contract",
        task_id="APF3b-contract",
        target_root=target_root,
        requested_scope="apf_backend",
        changed_files=["src/services/anonymous_preview_admission.py"],
        base_branch="main",
    )
    return result, target_root


def assert_contract_invalid_denial(result: Any, *, contains: str) -> None:
    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.denied_reasons)
    assert any(contains in reason for reason in result.denied_reasons)
    assert result.target_writes_performed is False
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False


@pytest.mark.parametrize(
    ("key", "value", "expected_type"),
    [
        ("allow_push", "false", "str"),
        ("allow_merge", "true", "str"),
        ("allow_write", 0, "int"),
        ("allow_deploy", 1, "int"),
        ("allow_network_write", [], "list"),
    ],
)
def test_d538_policy_mutation_flags_require_literal_bool(
    tmp_path: Path,
    key: str,
    value: Any,
    expected_type: str,
) -> None:
    config = build_test_config(tmp_path)
    config["policy"][key] = value

    result, target_root = run_plan(config, tmp_path)

    assert_contract_invalid_denial(result, contains=f"policy.{key}")
    assert any(expected_type in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)


def test_d538_missing_policy_mutation_flag_is_config_contract_invalid(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    del config["policy"]["allow_push"]

    result, target_root = run_plan(config, tmp_path)

    assert_contract_invalid_denial(result, contains="policy.allow_push is required and must be literal bool")
    assert_no_target_git_side_effects(target_root)


@pytest.mark.parametrize("policy_value", [[], ["not", "mapping"], None])
def test_d538_policy_section_must_be_mapping(tmp_path: Path, policy_value: Any) -> None:
    config = build_test_config(tmp_path)
    config["policy"] = policy_value

    result, target_root = run_plan(config, tmp_path)

    assert_contract_invalid_denial(result, contains="policy schema invalid")
    assert_no_target_git_side_effects(target_root)


@pytest.mark.parametrize(
    ("key", "value", "expected_type"),
    [
        ("enabled", "false", "str"),
        ("allow_auto_commit", "false", "str"),
        ("allow_auto_push", 0, "int"),
        ("allow_auto_pr", 1, "int"),
        ("allow_auto_merge", [], "list"),
    ],
)
def test_d538_git_mutation_flags_require_literal_bool(
    tmp_path: Path,
    key: str,
    value: Any,
    expected_type: str,
) -> None:
    config = build_test_config(tmp_path)
    config["git"][key] = value

    result, target_root = run_plan(config, tmp_path)

    assert_contract_invalid_denial(result, contains=f"git.{key}")
    assert any(expected_type in reason for reason in result.denied_reasons)
    assert_no_target_git_side_effects(target_root)


def test_d538_missing_git_mutation_flag_is_config_contract_invalid(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    del config["git"]["allow_auto_pr"]

    result, target_root = run_plan(config, tmp_path)

    assert_contract_invalid_denial(result, contains="git.allow_auto_pr is required and must be literal bool")
    assert_no_target_git_side_effects(target_root)


@pytest.mark.parametrize("git_value", [[], ["not", "mapping"], None])
def test_d538_git_section_must_be_mapping(tmp_path: Path, git_value: Any) -> None:
    config = build_test_config(tmp_path)
    config["git"] = git_value

    result, target_root = run_plan(config, tmp_path)

    assert_contract_invalid_denial(result, contains="git schema invalid")
    assert_no_target_git_side_effects(target_root)


def test_d538_literal_false_defaults_still_plan_without_git_side_effects(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "planned"
    assert result.dry_run is True
    assert result.target_writes_performed is False
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert_no_target_git_side_effects(target_root)


def test_d538_literal_true_mutation_flags_keep_existing_denial_reasons(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["allow_push"] = True
    config["policy"]["allow_merge"] = True
    config["git"]["allow_auto_commit"] = True

    result, target_root = run_plan(config, tmp_path)

    assert result.status == "policy_denied"
    assert result.denied_reasons == [
        "allow_push=true",
        "allow_merge=true",
        "git.allow_auto_commit=true",
    ]
    assert result.git_commit_performed is False
    assert result.git_push_performed is False
    assert result.git_merge_performed is False
    assert result.mcp_mutation_tools_exposed is False
    assert_no_target_git_side_effects(target_root)


def test_d538_cli_git_plan_reports_config_contract_invalid_for_quoted_false(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config["policy"]["allow_push"] = "false"
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "git-plan",
            "--config",
            str(config_path),
            "--plan-id",
            "D538-cli-contract",
            "--task-id",
            "APF3b-cli-contract",
            "--target-root",
            str(target_root),
            "--scope",
            "apf_backend",
            "--changed-file",
            "src/services/anonymous_preview_admission.py",
            "--dry-run",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in payload["denied_reasons"])
    assert any("policy.allow_push" in reason for reason in payload["denied_reasons"])
    assert payload["target_writes_performed"] is False
    assert payload["git_push_performed"] is False
    assert payload["git_merge_performed"] is False
    assert payload["mcp_mutation_tools_exposed"] is False
    assert_no_target_git_side_effects(target_root)

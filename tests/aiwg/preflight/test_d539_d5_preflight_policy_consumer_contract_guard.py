from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.d5_preflight import evaluate_d5_preflight

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    config["state_db"] = "docs/ai-workgroup/state/tasks.sqlite"
    config["artifact_root"] = "docs/ai-workgroup/state/artifacts"
    config["workflow_contract"] = {
        "topology_path": str(PROJECT_ROOT / "docs" / "ai-workgroup" / "topology" / "aiwg.topology.v1.yaml"),
        "workflow_path": str(PROJECT_ROOT / "docs" / "ai-workgroup" / "workflows" / "apf-preview-funnel.workflow.v1.yaml"),
        "read_only": True,
        "validate_only": True,
    }
    config["d5_preflight"] = {
        "budget": {"max_budget_usd": 0, "requested_budget_usd": 0},
        "lease": {"heartbeat_expected_seconds": 1200, "stale_after_seconds": 1800},
    }
    return config


def make_roots(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    (target_root / "README.md").write_text("protected target repo sentinel\n", encoding="utf-8")
    return project_root, target_root


def protected_repo_digest(target_root: Path) -> tuple[tuple[str, int], ...]:
    return tuple(
        sorted(
            (str(path.relative_to(target_root)).replace("\\", "/"), path.stat().st_size)
            for path in target_root.rglob("*")
            if path.is_file()
        )
    )


def assert_d539_safety_flags_false(snapshot: dict[str, Any]) -> None:
    assert snapshot["dry_run"] is True
    assert snapshot["fake_only"] is True
    assert snapshot["ready_for_real_agent_execution"] is False
    assert snapshot["ready_for_protected_business_repository_write"] is False
    assert snapshot["target_writes_performed"] is False
    assert snapshot["mcp_mutation_tools_exposed"] is False
    assert snapshot["github_write_api_called"] is False
    assert snapshot["pr_comment_performed"] is False
    assert snapshot["pr_mutation_performed"] is False
    assert snapshot["created_fix_tasks"] is False
    assert snapshot["codex_automation_modified"] is False
    assert snapshot["git_push_performed"] is False
    assert snapshot["git_merge_performed"] is False
    assert snapshot["git_deploy_performed"] is False
    assert snapshot["real_agents_started"] is False
    assert snapshot["real_processes_started"] is False


def assert_contract_invalid_snapshot(
    snapshot: dict[str, Any],
    *,
    contains: str,
    project_root: Path,
    target_root: Path,
) -> None:
    assert snapshot["status"] == "blocked"
    assert any("config_contract_invalid" in reason for reason in snapshot["policy_denials"])
    assert any(contains in reason for reason in snapshot["policy_denials"])
    assert_d539_safety_flags_false(snapshot)
    artifact_path = Path(snapshot["artifact_path"])
    assert artifact_path.exists()
    assert artifact_path.resolve().is_relative_to(project_root.resolve())
    assert not artifact_path.resolve().is_relative_to(target_root.resolve())
    assert not (project_root / "docs" / "ai-workgroup" / "state" / "tasks.sqlite").exists()


def run_preflight(config: dict[str, Any], *, project_root: Path, target_root: Path) -> dict[str, Any]:
    return evaluate_d5_preflight(
        config=config,
        project_root=project_root,
        workflow_id="apf-preview-funnel",
        target_root=target_root,
        dry_run=True,
    )


@pytest.mark.parametrize("policy_value", [None, [], ["not", "mapping"]])
def test_d539_policy_section_must_be_mapping_before_db_setup(
    tmp_path: Path,
    policy_value: Any,
) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    config["policy"] = policy_value
    before = protected_repo_digest(target_root)

    snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    assert_contract_invalid_snapshot(
        snapshot,
        contains="policy schema invalid: policy must be a mapping",
        project_root=project_root,
        target_root=target_root,
    )
    assert protected_repo_digest(target_root) == before


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        ("false", "str"),
        ("true", "str"),
        (0, "int"),
        (1, "int"),
        ([], "list"),
    ],
)
def test_d539_policy_forbidden_keys_require_literal_bool(
    tmp_path: Path,
    value: Any,
    expected_type: str,
) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    config["policy"]["allow_write"] = value
    before = protected_repo_digest(target_root)

    snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    assert_contract_invalid_snapshot(
        snapshot,
        contains="policy.allow_write",
        project_root=project_root,
        target_root=target_root,
    )
    assert any(expected_type in reason for reason in snapshot["policy_denials"])
    assert protected_repo_digest(target_root) == before


def test_d539_missing_policy_forbidden_key_is_config_contract_invalid(tmp_path: Path) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    del config["policy"]["allow_write"]
    before = protected_repo_digest(target_root)

    snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    assert_contract_invalid_snapshot(
        snapshot,
        contains="policy.allow_write is required and must be literal bool",
        project_root=project_root,
        target_root=target_root,
    )
    assert protected_repo_digest(target_root) == before


def test_d539_top_level_false_and_absent_remain_compatible(tmp_path: Path) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)

    absent_snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    project_root_with_false, target_root_with_false = make_roots(tmp_path / "top-level-false")
    config_with_false = build_test_config(project_root_with_false)
    config_with_false["allow_write"] = False
    false_snapshot = run_preflight(
        config_with_false,
        project_root=project_root_with_false,
        target_root=target_root_with_false,
    )

    assert absent_snapshot["status"] == "passed_dry_run"
    assert "allow_write" not in absent_snapshot["policy_denials"]
    assert false_snapshot["status"] == "passed_dry_run"
    assert "allow_write" not in false_snapshot["policy_denials"]
    assert_d539_safety_flags_false(absent_snapshot)
    assert_d539_safety_flags_false(false_snapshot)


def test_d539_top_level_true_preserves_existing_denial(tmp_path: Path) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    config["allow_write"] = True

    snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    assert snapshot["status"] == "blocked"
    assert "allow_write" in snapshot["policy_denials"]
    assert not any("config_contract_invalid" in reason for reason in snapshot["policy_denials"])
    assert_d539_safety_flags_false(snapshot)


@pytest.mark.parametrize(("value", "expected_type"), [("false", "str"), (0, "int"), ([], "list")])
def test_d539_top_level_forbidden_key_if_present_must_be_literal_bool(
    tmp_path: Path,
    value: Any,
    expected_type: str,
) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    config["allow_write"] = value
    before = protected_repo_digest(target_root)

    snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    assert_contract_invalid_snapshot(
        snapshot,
        contains="allow_write must be literal bool when present",
        project_root=project_root,
        target_root=target_root,
    )
    assert any(expected_type in reason for reason in snapshot["policy_denials"])
    assert protected_repo_digest(target_root) == before


def test_d539_literal_true_policy_values_keep_existing_denial_reasons(tmp_path: Path) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    config["policy"]["allow_real_agents"] = True
    config["policy"]["allow_push"] = True

    snapshot = run_preflight(config, project_root=project_root, target_root=target_root)

    assert snapshot["status"] == "blocked"
    assert snapshot["policy_denials"] == ["policy.allow_real_agents", "policy.allow_push"]
    assert_d539_safety_flags_false(snapshot)


def test_d539_cli_fail_on_blocked_reports_config_contract_invalid_for_numeric_policy(
    tmp_path: Path,
) -> None:
    project_root, target_root = make_roots(tmp_path)
    config = build_test_config(project_root)
    config["policy"]["allow_write"] = 0
    config_path = project_root / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "d5-preflight",
            "--config",
            str(config_path),
            "--workflow-id",
            "apf-preview-funnel",
            "--target-root",
            str(target_root),
            "--dry-run",
            "--json",
            "--fail-on-blocked",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 3
    payload = json.loads(completed.stdout)
    assert_contract_invalid_snapshot(
        payload,
        contains="policy.allow_write",
        project_root=project_root,
        target_root=target_root,
    )
    assert any("int" in reason for reason in payload["policy_denials"])
    assert completed.stderr == ""

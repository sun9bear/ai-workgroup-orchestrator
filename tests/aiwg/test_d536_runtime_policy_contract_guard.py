from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config
from aiwg.policy import evaluate_runtime_policy


def _decision_for_policy(tmp_path: Path, policy_value: Any):
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"] = policy_value
    return evaluate_runtime_policy(
        config=config,
        project_root=tmp_path,
        agent="Fake",
        adapter_type="fake",
        task={"can_write": False, "requires_human": False},
    )


def test_d536_runtime_policy_denies_malformed_policy_mapping(tmp_path: Path) -> None:
    decision = _decision_for_policy(tmp_path, [])

    assert decision.allowed is False
    assert any("config_contract_invalid" in reason for reason in decision.reasons)
    assert any("policy must be a mapping" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("safe_mode", "false"),
        ("allow_real_agents", "false"),
        ("allow_external_agents", 0),
        ("allow_write", 0),
    ],
)
def test_d536_runtime_policy_rejects_non_literal_policy_bools(
    tmp_path: Path,
    key: str,
    bad_value: Any,
) -> None:
    config = build_default_config(project_root=tmp_path)
    policy = copy.deepcopy(config["policy"])
    policy[key] = bad_value

    decision = _decision_for_policy(tmp_path, policy)

    assert decision.allowed is False
    assert any("config_contract_invalid" in reason for reason in decision.reasons)
    assert any(f"policy.{key}" in reason for reason in decision.reasons)


def test_d536_runtime_policy_literal_true_authorization_values_are_not_rejected_by_safe_default_contract(
    tmp_path: Path,
) -> None:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["agents"]["OpenCode"]["enabled"] = True
    config["policy"]["safe_mode"] = False
    config["policy"]["allow_real_agents"] = True
    config["policy"]["allow_external_agents"] = True

    decision = evaluate_runtime_policy(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        adapter_type="opencode",
        task={"can_write": False, "requires_human": False},
    )

    assert decision.allowed is True
    assert not any("config_contract_invalid" in reason for reason in decision.reasons)

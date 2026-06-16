from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from aiwg.config import build_default_config, validate_config_contract
from aiwg.d5_preflight import FORBIDDEN_POLICY_KEYS
from aiwg.doctor import run_doctor


FORBIDDEN_FALSE_KEYS = tuple(FORBIDDEN_POLICY_KEYS)


def _config_with_policy_value(key: str, value: Any) -> dict[str, Any]:
    config = copy.deepcopy(build_default_config())
    policy = config["policy"]
    policy[key] = value
    return config


def _config_without_policy_key(key: str) -> dict[str, Any]:
    config = copy.deepcopy(build_default_config())
    del config["policy"][key]
    return config


def test_d535_validator_accepts_default_policy_safety_contract() -> None:
    result = validate_config_contract(build_default_config())

    assert result.ok is True
    assert any("policy safety schema ok" in message for message in result.messages)


@pytest.mark.parametrize("policy_value", [None, [], "", "policy", 0, True, object()])
def test_d535_validator_rejects_malformed_policy_mapping(policy_value: Any) -> None:
    config = build_default_config()
    config["policy"] = policy_value

    result = validate_config_contract(config)

    assert result.ok is False
    assert any("policy" in error for error in result.errors)


@pytest.mark.parametrize("bad_value", [False, None, "true", "false", "1", 1, [], {}, object()])
def test_d535_validator_requires_safe_mode_literal_true(bad_value: Any) -> None:
    result = validate_config_contract(_config_with_policy_value("safe_mode", bad_value))

    assert result.ok is False
    assert any("policy.safe_mode" in error for error in result.errors)


@pytest.mark.parametrize("key", FORBIDDEN_FALSE_KEYS)
@pytest.mark.parametrize("bad_value", [True, None, "false", "0", 0, [], {}, object()])
def test_d535_validator_requires_forbidden_switches_literal_false(key: str, bad_value: Any) -> None:
    result = validate_config_contract(_config_with_policy_value(key, bad_value))

    assert result.ok is False
    assert any(f"policy.{key}" in error for error in result.errors)


@pytest.mark.parametrize("key", FORBIDDEN_FALSE_KEYS)
def test_d535_validator_rejects_missing_forbidden_switches(key: str) -> None:
    result = validate_config_contract(_config_without_policy_key(key))

    assert result.ok is False
    assert any(f"policy.{key}" in error for error in result.errors)


def test_d535_run_doctor_rejects_numeric_false_policy_value(tmp_path: Path) -> None:
    config = _config_with_policy_value("allow_write", 0)
    config_path = tmp_path / "aiwg.invalid-policy.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is False
    assert any("policy.allow_write" in error for error in result.errors)


def test_d535_cli_doctor_rejects_numeric_false_policy_value(tmp_path: Path) -> None:
    config = _config_with_policy_value("allow_write", 0)
    config_path = tmp_path / "aiwg.invalid-policy.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "doctor",
            "--config",
            str(config_path),
            "--project-root",
            str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "AIWG doctor: FAILED" in combined
    assert "policy.allow_write" in combined

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

import yaml

from aiwg import config as config_module
from aiwg.config import Config, build_default_config, dump_config
from aiwg.doctor import run_doctor


def _validator() -> Callable[[Config], object]:
    validator = getattr(config_module, "validate_config_contract", None)
    assert callable(validator), "missing unified validate_config_contract(config) API"
    return validator


def test_d534_unified_config_validator_accepts_default_contract() -> None:
    result = _validator()(build_default_config())

    assert getattr(result, "ok") is True
    assert getattr(result, "errors") == []
    assert any("protected_target_roots" in message for message in getattr(result, "messages"))


def test_d534_unified_config_validator_fails_closed_on_malformed_protected_target_roots() -> None:
    config = build_default_config()
    config["protected_target_roots"] = {"target": "D:/example/protected-business-repo"}

    result = _validator()(config)

    assert getattr(result, "ok") is False
    assert any("protected_target_roots" in error for error in getattr(result, "errors"))


def test_d534_doctor_reports_malformed_protected_target_roots_as_config_schema_error(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)
    config["protected_target_roots"] = [["D:/nested/business"]]
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is False
    assert any("protected_target_roots" in error for error in result.errors)


def test_d534_cli_doctor_fails_closed_for_invalid_protected_target_roots(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)
    config["protected_target_roots"] = None
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

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
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 1
    assert "AIWG doctor: FAILED" in completed.stdout
    assert "protected_target_roots" in completed.stdout

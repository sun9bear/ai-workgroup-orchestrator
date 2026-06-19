from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.adapter_binary_readiness import resolve_adapter_binary_readiness
from aiwg.config import build_default_config, dump_config
from aiwg.doctor import run_doctor

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _base_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["artifact_root"] = str(tmp_path / "docs" / "ai-workgroup" / "state" / "artifacts")
    config["state_db"] = str(tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite")
    config["workgroup_root"] = str(tmp_path / "docs" / "ai-workgroup")
    config["protected_target_roots"] = []
    return config


def _python_marker_args(marker_path: Path) -> list[str]:
    return [
        "-c",
        "from pathlib import Path; "
        f"Path({str(marker_path)!r}).write_text('version-probe-ran', encoding='utf-8')",
    ]


def test_missing_adapter_binary_readiness_uses_safe_defaults_without_probe(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config.pop("adapter_binary_readiness", None)

    report = resolve_adapter_binary_readiness(
        config=config,
        project_root=tmp_path,
        run_version_probes=True,
    )

    assert report["policy"]["version_probe_enabled"] is False
    assert report["policy"]["configured_auto_install"] is False
    assert report["policy"]["configured_auto_login"] is False
    assert report["policy"]["configured_read_tokens"] is False
    assert report["safety"]["started_version_probe_process"] is False
    assert report["safety"]["auto_install"] is False
    assert report["safety"]["auto_login"] is False
    assert report["safety"]["read_tokens"] is False


def test_non_mapping_adapter_binary_readiness_fails_closed_before_probe(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    config["adapter_binary_readiness"] = ["not", "a", "mapping"]

    with pytest.raises(ValueError, match="config_contract_invalid: adapter_binary_readiness must be a mapping"):
        resolve_adapter_binary_readiness(
            config=config,
            project_root=tmp_path,
            run_version_probes=True,
        )


def test_global_version_probe_enabled_string_false_fails_before_subprocess(tmp_path: Path) -> None:
    marker = tmp_path / "global-version-probe-ran.txt"
    config = _base_config(tmp_path)
    config["adapter_binary_readiness"] = {
        "version_probe_enabled": "false",
        "adapters": {
            "opencode": {
                "path": sys.executable,
                "version_args": _python_marker_args(marker),
            }
        },
    }

    with pytest.raises(
        ValueError,
        match="config_contract_invalid: adapter_binary_readiness.version_probe_enabled must be literal bool; got str",
    ):
        resolve_adapter_binary_readiness(
            config=config,
            project_root=tmp_path,
            run_version_probes=True,
        )

    assert not marker.exists(), "malformed string false must not start a version-probe subprocess"


def test_per_adapter_version_probe_enabled_string_false_fails_before_subprocess(tmp_path: Path) -> None:
    marker = tmp_path / "per-adapter-version-probe-ran.txt"
    config = _base_config(tmp_path)
    config["adapter_binary_readiness"] = {
        "version_probe_enabled": False,
        "adapters": {
            "opencode": {
                "path": sys.executable,
                "version_probe_enabled": "false",
                "version_args": _python_marker_args(marker),
            }
        },
    }

    with pytest.raises(
        ValueError,
        match=(
            "config_contract_invalid: "
            "adapter_binary_readiness.adapters.opencode.version_probe_enabled must be literal bool; got str"
        ),
    ):
        resolve_adapter_binary_readiness(
            config=config,
            project_root=tmp_path,
            run_version_probes=True,
        )

    assert not marker.exists(), "malformed per-adapter string false must not start a version-probe subprocess"


@pytest.mark.parametrize(
    ("key", "value", "type_name"),
    [
        ("auto_install", "false", "str"),
        ("auto_login", 0, "int"),
        ("read_tokens", None, "NoneType"),
    ],
)
def test_doctor_rejects_non_literal_auto_action_flags(
    tmp_path: Path,
    key: str,
    value: Any,
    type_name: str,
) -> None:
    config = _base_config(tmp_path)
    config["adapter_binary_readiness"].update(
        {
            "auto_install": False,
            "auto_login": False,
            "read_tokens": False,
            key: value,
        }
    )
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is False
    assert (
        f"config_contract_invalid: adapter_binary_readiness.{key} must be literal bool; got {type_name}"
        in result.errors
    )


def test_adapter_readiness_cli_malformed_version_probe_reports_blocked_without_subprocess(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cli-version-probe-ran.txt"
    config = _base_config(tmp_path)
    config["adapter_binary_readiness"] = {
        "version_probe_enabled": "false",
        "adapters": {
            "opencode": {
                "path": sys.executable,
                "version_args": _python_marker_args(marker),
            }
        },
    }
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "adapter-readiness", "--config", str(config_path), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    doc = json.loads(completed.stdout)
    assert doc["status"] == "blocked"
    assert doc["error"] == "config_contract_invalid"
    assert "config_contract_invalid: adapter_binary_readiness.version_probe_enabled must be literal bool; got str" in doc["errors"]
    assert doc["safety"]["started_version_probe_process"] is False
    assert doc["safety"]["started_real_agent_task_process"] is False
    assert doc["safety"]["started_adapter_process"] is False
    assert not marker.exists(), "blocked adapter-readiness CLI must not start version-probe subprocess"

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import resolve_adapter_binary_readiness
from aiwg.config import build_default_config, dump_config
from aiwg.doctor import run_doctor

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b12-secret-token-should-never-appear"


def build_readiness_config(tmp_path: Path, *, version_probe_enabled: bool = True) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["real_adapter_env"] = {"OPENAI_API_KEY": SECRET_VALUE}
    config["adapter_binary_readiness"] = {
        "enabled": True,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "version_probe_enabled": version_probe_enabled,
        "version_probe_timeout_seconds": 3,
        "adapters": {
            "opencode": {
                "path": sys.executable,
                "version_args": ["-c", "print('OpenCode 9.8.7')"],
                "version_probe_enabled": version_probe_enabled,
            },
            "codex_cli": {
                "path": str(tmp_path / "missing-codex-binary.exe"),
                "version_args": ["--version"],
                "version_probe_enabled": version_probe_enabled,
            },
        },
    }
    return config


def test_resolver_reports_available_binary_version_without_secrets_or_side_effects(tmp_path: Path) -> None:
    config = build_readiness_config(tmp_path)

    report = resolve_adapter_binary_readiness(
        config=config,
        project_root=tmp_path,
        run_version_probes=True,
    )

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert SECRET_VALUE not in encoded
    assert report["schema_version"] == "aiwg.adapter_binary_readiness.v1"
    assert report["mode"] == "read_only_binary_resolver"
    assert report["safety"] == {
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "values_recorded": False,
        "started_version_probe_process": True,
        "started_real_agent_task_process": False,
        "started_adapter_process": False,
        "desktop_automation_allowed": False,
        "codex_automation_modification_allowed": False,
    }
    opencode = report["adapters"]["opencode"]
    assert opencode["available"] is True
    assert opencode["readiness"] == "ready"
    assert opencode["binary_name"] == "opencode"
    assert Path(opencode["resolved_path"]) == Path(sys.executable)
    assert opencode["version"] == "OpenCode 9.8.7"
    assert opencode["version_probe"]["enabled"] is True
    assert opencode["version_probe"]["started_process"] is True
    assert opencode["version_probe"]["exit_code"] == 0
    assert opencode["started_adapter_process"] is False
    assert opencode["install_action"] == "not_attempted"
    assert opencode["login_action"] == "not_attempted"
    assert opencode["token_files_read"] is False


def test_missing_binary_is_reported_without_probe_or_install_login(tmp_path: Path) -> None:
    config = build_readiness_config(tmp_path)

    report = resolve_adapter_binary_readiness(
        config=config,
        project_root=tmp_path,
        run_version_probes=True,
    )

    codex = report["adapters"]["codex_cli"]
    assert codex["available"] is False
    assert codex["readiness"] == "missing"
    assert codex["resolved_path"] is None
    assert codex["version"] is None
    assert codex["version_probe"]["enabled"] is True
    assert codex["version_probe"]["started_process"] is False
    assert codex["version_probe"]["skipped_reason"] == "binary_missing"
    assert codex["install_action"] == "not_attempted"
    assert codex["login_action"] == "not_attempted"
    assert codex["token_files_read"] is False
    assert codex["codex"]["desktop_automation_allowed"] is False
    assert codex["codex"]["automation_modification_policy"] == "forbidden_without_explicit_user_authorization"


def test_adapter_readiness_cli_writes_report_event_and_status_json_snapshot(tmp_path: Path) -> None:
    config = build_readiness_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    init_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "init-db", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    readiness_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "adapter-readiness", "--config", str(config_path), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json", "--recent-events", "5"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert init_completed.returncode == 0, init_completed.stderr
    assert readiness_completed.returncode == 0, readiness_completed.stderr
    readiness_doc = json.loads(readiness_completed.stdout)
    assert readiness_doc["summary"]["available"] >= 1
    assert readiness_doc["summary"]["missing"] >= 1
    report_path = Path(readiness_doc["report_path"])
    assert report_path.exists()
    assert SECRET_VALUE not in report_path.read_text(encoding="utf-8")
    assert status_completed.returncode == 0, status_completed.stderr
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["adapter_readiness"]["report_path"] == str(report_path)
    assert snapshot["adapter_readiness"]["summary"] == readiness_doc["summary"]
    assert snapshot["agent_runs"] == []
    assert snapshot["recent_events"][0]["type"] == "adapter_binary_readiness_checked"
    assert snapshot["recent_events"][0]["payload"]["started_adapter_process"] is False
    assert snapshot["recent_events"][0]["payload"]["auto_install"] is False
    assert snapshot["recent_events"][0]["payload"]["auto_login"] is False
    assert snapshot["recent_events"][0]["payload"]["read_tokens"] is False


def test_doctor_surfaces_readiness_policy_and_blocks_unsafe_install_login_token_flags(tmp_path: Path) -> None:
    safe_config = build_readiness_config(tmp_path, version_probe_enabled=False)
    safe_path = tmp_path / "safe-aiwg.yaml"
    safe_path.write_text(dump_config(safe_config), encoding="utf-8")

    safe_result = run_doctor(config_path=safe_path, project_root=tmp_path)

    assert safe_result.ok is True
    assert any("adapter_binary_readiness" in message for message in safe_result.messages)
    assert any("auto_install=false" in message for message in safe_result.messages)
    assert any("auto_login=false" in message for message in safe_result.messages)
    assert any("read_tokens=false" in message for message in safe_result.messages)

    unsafe_config = build_readiness_config(tmp_path, version_probe_enabled=False)
    unsafe_config["adapter_binary_readiness"].update(
        {"auto_install": True, "auto_login": True, "read_tokens": True}
    )
    unsafe_path = tmp_path / "unsafe-aiwg.yaml"
    unsafe_path.write_text(dump_config(unsafe_config), encoding="utf-8")

    unsafe_result = run_doctor(config_path=unsafe_path, project_root=tmp_path)

    assert unsafe_result.ok is False
    assert "adapter_binary_readiness.auto_install must remain false" in unsafe_result.errors
    assert "adapter_binary_readiness.auto_login must remain false" in unsafe_result.errors
    assert "adapter_binary_readiness.read_tokens must remain false" in unsafe_result.errors

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from aiwg.config import build_default_config, write_default_config
from aiwg.doctor import run_doctor


def test_default_config_uses_safe_phase_a0_values(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)

    assert config["project_root"] == "."
    assert config["workgroup_root"] == "docs/ai-workgroup"
    assert config["state_db"] == "docs/ai-workgroup/state/tasks.sqlite"

    assert config["agents"]["Fake"]["enabled"] is True
    assert config["agents"]["Fake"]["can_write"] is False
    for real_agent in ("OpenCode", "Claude-Code", "Codex", "Hermes"):
        assert config["agents"][real_agent]["enabled"] is False

    policy = config["policy"]
    assert policy["safe_mode"] is True
    assert policy["allow_real_agents"] is False
    assert policy["allow_real_adapter_dispatch"] is False
    assert policy["allow_real_process_execution"] is False
    assert policy["allow_write"] is False
    assert policy["allow_push"] is False
    assert policy["allow_merge"] is False
    assert policy["allow_deploy"] is False
    assert policy["allow_modify_codex_automations"] is False

    git = config["git"]
    assert git["enabled"] is False
    assert git["allow_auto_commit"] is False
    assert git["allow_auto_push"] is False
    assert git["allow_auto_pr"] is False
    assert git["allow_auto_merge"] is False

    migration = config["legacy_migration"]
    assert migration["mode"] == "audit_only"
    assert migration["import_ready"] is False
    assert migration["require_human_selection"] is True


def test_write_default_config_creates_readable_aiwg_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "aiwg.yaml"

    write_default_config(config_path, project_root=tmp_path)

    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert loaded["policy"]["safe_mode"] is True
    assert loaded["policy"]["allow_real_agents"] is False
    assert loaded["policy"]["allow_real_adapter_dispatch"] is False
    assert loaded["policy"]["allow_real_process_execution"] is False
    assert loaded["policy"]["allow_modify_codex_automations"] is False


def test_doctor_accepts_safe_defaults_and_reports_no_git_repo(tmp_path: Path) -> None:
    config_path = tmp_path / "aiwg.yaml"
    write_default_config(config_path, project_root=tmp_path)

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is True
    assert any("safe_mode=true" in message for message in result.messages)
    assert any("allow_real_agents=false" in message for message in result.messages)
    assert any("allow_real_process_execution=false" in message for message in result.messages)
    assert any("not a git repository" in message for message in result.warnings)
    assert not (tmp_path / ".git").exists()


def test_doctor_fails_if_real_agents_are_enabled_without_permission(tmp_path: Path) -> None:
    config_path = tmp_path / "aiwg.yaml"
    config = build_default_config(project_root=tmp_path)
    config["policy"]["allow_real_agents"] = True
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is False
    assert any("allow_real_agents must remain false for Phase A0" in error for error in result.errors)


def test_doctor_fails_if_real_process_execution_is_enabled_without_permission(tmp_path: Path) -> None:
    config_path = tmp_path / "aiwg.yaml"
    config = build_default_config(project_root=tmp_path)
    config["policy"]["allow_real_process_execution"] = True
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is False
    assert any("allow_real_process_execution must remain false" in error for error in result.errors)


def test_cli_help_runs_from_project_root() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "--help"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0
    assert "AI Workgroup Orchestrator" in completed.stdout
    assert "doctor" in completed.stdout
    assert "init-config" in completed.stdout

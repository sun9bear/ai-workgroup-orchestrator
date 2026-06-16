from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.mcp.tools import status_tool
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STALE_CHECKED_AT = "2026-06-01T00:00:00+00:00"
EXPECTED_WARNING = {
    "code": "adapter_readiness_stale",
    "severity": "warning",
    "message": (
        "Adapter readiness is stale and runtime-only; do not use it to authorize real agent "
        "startup. Re-run adapter-readiness and real-mode preflight before any real agent start."
    ),
    "action": "rerun_adapter_readiness_and_preflight_before_real_agent_start",
    "blocks_real_agent_start": True,
    "read_only": True,
}


def build_c4_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["adapter_binary_readiness"] = {
        "enabled": True,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "version_probe_enabled": False,
        "adapters": {"opencode": {"path": sys.executable, "version_probe_enabled": False}},
    }
    config["adapter_readiness_gate"] = {
        "enabled": True,
        "max_age_minutes": 1,
        "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
    }
    return config


def write_config(tmp_path: Path, config: dict[str, Any]) -> Path:
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    return config_path


def db_digest(db_path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(db_path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            conn.execute("SELECT id, type, status, created_at FROM events ORDER BY id").fetchall(),
        )


def seed_stale_adapter_readiness(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    config = build_c4_config(tmp_path)
    config_path = write_config(tmp_path, config)
    db_path = init_database(config=config, project_root=tmp_path)
    report = write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    assert report["safety"]["started_real_agent_task_process"] is False
    assert report["safety"]["started_adapter_process"] is False
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET created_at = ? WHERE type = 'adapter_binary_readiness_checked'",
            (STALE_CHECKED_AT,),
        )
    return config, config_path, db_path


def stable_warning_fields(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "code": warning.get("code"),
            "severity": warning.get("severity"),
            "message": warning.get("message"),
            "action": warning.get("action"),
            "blocks_real_agent_start": warning.get("blocks_real_agent_start"),
            "read_only": warning.get("read_only"),
        }
        for warning in payload.get("warnings", [])
    ]


def test_status_snapshot_promotes_stale_readiness_to_strong_read_only_warning(tmp_path: Path) -> None:
    config, _config_path, db_path = seed_stale_adapter_readiness(tmp_path)
    before = db_digest(db_path)

    snapshot = get_status_snapshot(config=config, project_root=tmp_path, recent_events=5)
    text = render_status_text(snapshot)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["capabilities"]["read_only"] is True
    assert snapshot["capabilities"]["mutation_actions"] == []
    assert snapshot["adapter_readiness"]["stale"] is True
    assert snapshot["adapter_readiness"]["stale_reason"] == "adapter_readiness_report_stale"
    assert snapshot["warnings"] == [EXPECTED_WARNING]
    assert snapshot["adapter_readiness"]["warning"] == EXPECTED_WARNING
    assert "Warnings" in text
    assert "adapter_readiness_stale" in text
    assert "runtime-only" in text
    assert "real agent" in text
    assert "preflight" in text


def test_cli_status_json_and_text_expose_same_stale_warning_without_mutation(tmp_path: Path) -> None:
    _config, config_path, db_path = seed_stale_adapter_readiness(tmp_path)
    before = db_digest(db_path)

    json_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    after = db_digest(db_path)

    assert before == after
    assert json_completed.returncode == 0, json_completed.stderr
    payload = json.loads(json_completed.stdout)
    assert payload["warnings"] == [EXPECTED_WARNING]
    assert payload["adapter_readiness"]["warning"] == EXPECTED_WARNING
    assert text_completed.returncode == 0, text_completed.stderr
    assert "Warnings" in text_completed.stdout
    assert "adapter_readiness_stale" in text_completed.stdout
    assert "runtime-only" in text_completed.stdout
    assert "preflight" in text_completed.stdout


def test_mcp_status_warning_matches_cli_status_warning(tmp_path: Path) -> None:
    _config, config_path, db_path = seed_stale_adapter_readiness(tmp_path)
    before = db_digest(db_path)

    cli_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    mcp_payload = status_tool(config_path=config_path, recent_events=5, task_limit=5)
    after = db_digest(db_path)

    assert before == after
    assert cli_completed.returncode == 0, cli_completed.stderr
    cli_payload = json.loads(cli_completed.stdout)
    assert stable_warning_fields(mcp_payload) == stable_warning_fields(cli_payload) == [EXPECTED_WARNING]
    assert mcp_payload["adapter_readiness"]["warning"] == cli_payload["adapter_readiness"]["warning"]
    assert mcp_payload["capabilities"]["read_only"] is True
    assert mcp_payload["capabilities"]["mutation_actions"] == []

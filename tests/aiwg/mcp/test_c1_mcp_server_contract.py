from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from aiwg.config import build_default_config, dump_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def write_config(tmp_path: Path) -> Path:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config), encoding="utf-8")
    return path


def test_server_help_lists_only_phase_c_read_only_tools() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.mcp.server", "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Phase C read-only MCP server" in completed.stdout
    for tool_name in ("status", "list_tasks", "get_task", "recent_events"):
        assert tool_name in completed.stdout
    for forbidden in ("claim", "update_status", "approve", "write_message", "start_real_agent"):
        assert forbidden not in completed.stdout


def test_server_contract_exposes_exact_read_only_tool_names() -> None:
    from aiwg.mcp.server import READ_ONLY_TOOL_NAMES, build_parser

    assert READ_ONLY_TOOL_NAMES == ("status", "list_tasks", "get_task", "recent_events")
    parser_help = build_parser().format_help()
    assert "read-only" in parser_help
    assert "claim_message" not in parser_help
    assert "write_message" not in parser_help


def test_list_tools_cli_does_not_require_mcp_sdk_or_create_database(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"

    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.mcp.server", "--config", str(config_path), "--list-tools"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert "status" in completed.stdout
    assert "list_tasks" in completed.stdout
    assert "get_task" in completed.stdout
    assert "recent_events" in completed.stdout
    assert db_path.exists() is False


def test_start_without_mcp_sdk_fails_clearly_without_mutation(tmp_path: Path, monkeypatch) -> None:
    from aiwg.mcp import server

    config_path = write_config(tmp_path)
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"

    def missing_sdk():
        raise RuntimeError(server.MCP_SDK_ERROR)

    monkeypatch.setattr(server, "_load_fastmcp", missing_sdk)

    exit_code = server.main(["--config", str(config_path), "--require-sdk-check-only"])

    assert exit_code == 2
    assert db_path.exists() is False

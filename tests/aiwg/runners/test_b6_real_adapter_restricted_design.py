from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database
from aiwg.state.importer import list_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_real_adapter_config(tmp_path: Path, *, agent: str = "OpenCode", allow_write: bool = False) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_write": allow_write,
            "allow_real_adapter_dispatch": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["agents"][agent]["enabled"] = True
    return config


def yaml_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_message(
    project_root: Path,
    *,
    agent: str,
    to_agent: str,
    message_id: str,
    task: str | None = None,
    can_write: bool = False,
    allowed_files: list[str] | None = None,
    context_files: list[str] | None = None,
    acceptance: list[str] | None = None,
) -> Path:
    task_id = task or message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / agent
        / f"2026-06-05T160000_from-CodeX_to-{to_agent}_type-instruction_task-{task_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    allowed_files = [] if allowed_files is None else allowed_files
    context_files = ["README.md"] if context_files is None else context_files
    acceptance = [] if acceptance is None else acceptance
    lines = [
        "---",
        f"id: {message_id}",
        f"task: {task_id}",
        "from: CodeX",
        f"to: {to_agent}",
        "type: instruction",
        "status: ready",
        "priority: medium",
        'reply_to: ""',
        "requires_human: false",
        "created_at: 2026-06-05T16:00:00+08:00",
        f"can_write: {str(can_write).lower()}",
        "context_files:",
    ]
    if context_files:
        lines.extend(f"  - {item}" for item in context_files)
    else:
        lines[-1] = "context_files: []"
    lines.append("allowed_files:")
    if allowed_files:
        lines.extend(f"  - {item}" for item in allowed_files)
    else:
        lines[-1] = "allowed_files: []"
    lines.extend(["forbidden_files:", "  - .env"])
    if acceptance:
        lines.append("acceptance:")
        lines.extend(f"  - {yaml_single_quoted(command)}" for command in acceptance)
    else:
        lines.append("acceptance: []")
    lines.extend(
        [
            'claimed_by: ""',
            'claimed_at: ""',
            'lock_id: ""',
            "attempt: 0",
            "max_attempts: 2",
            "timeout_minutes: 30",
            "review_delegate: CodeX",
            "---",
            "",
            "# B6 real adapter restricted design fixture",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def latest_event_payload(db_path: Path, message_id: str, event_type: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM events
            WHERE message_id = ? AND type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id, event_type),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_default_real_adapter_specs_are_preflight_only_and_codex_automation_locked(tmp_path: Path) -> None:
    from aiwg.adapter_registry import get_adapter_spec

    config = build_default_config(project_root=tmp_path)

    assert config["policy"]["allow_real_adapter_dispatch"] is False
    for adapter_type in ("opencode", "claude_code", "codex_cli", "hermes_bridge"):
        spec = get_adapter_spec(adapter_type)
        assert spec.adapter_type == adapter_type
        assert spec.real is True
        assert spec.invocation_mode in {"cli", "bridge"}
        assert spec.command_template
        assert "start_real_agent_process" in spec.forbidden_side_effects

    codex = get_adapter_spec("codex_cli")
    assert codex.codex_desktop_automation is False
    assert "modify_codex_desktop_automations" in codex.forbidden_side_effects


def test_real_adapter_preflight_writes_manifest_without_claim_or_agent_run(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path, agent="OpenCode")
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        agent="OpenCode",
        to_agent="OpenCode",
        message_id="B6-msg-opencode-preflight",
        context_files=["README.md", "docs/ai-workgroup/00-protocol.md"],
        acceptance=[f'"{sys.executable}" -c "print(123)"'],
    )

    result = run_once(config=config, project_root=tmp_path, agent="OpenCode")

    assert result.status == "adapter_preflight_required"
    assert result.message_id == "B6-msg-opencode-preflight"
    assert result.manifest_path is not None
    assert db_rows(db_path, "SELECT id, status, attempt, claimed_by FROM tasks") == [
        ("B6-msg-opencode-preflight", "waiting_human", 0, None)
    ]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT type, status FROM events ORDER BY id") == [
        ("task_imported", "ready"),
        ("adapter_preflight_required", "waiting_human"),
    ]

    payload = latest_event_payload(db_path, "B6-msg-opencode-preflight", "adapter_preflight_required")
    manifest_path = Path(payload["manifest_path"])
    assert manifest_path == result.manifest_path
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["phase"] == "B6-real-adapter-restricted-design"
    assert manifest["mode"] == "preflight_only"
    assert manifest["dispatch_allowed"] is False
    assert manifest["agent"] == "OpenCode"
    assert manifest["adapter_type"] == "opencode"
    assert manifest["task"]["message_id"] == "B6-msg-opencode-preflight"
    assert manifest["task"]["can_write"] is False
    assert manifest["task"]["context_files"] == ["README.md", "docs/ai-workgroup/00-protocol.md"]
    assert manifest["task"]["acceptance"] == [f'"{sys.executable}" -c "print(123)"']
    assert "runtime_policy" in manifest["required_gates"]
    assert "verification_gate" in manifest["required_gates"]
    assert "start_real_agent_process" in manifest["forbidden_side_effects"]


def test_codex_cli_preflight_preserves_desktop_automation_lock(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path, agent="Codex")
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        agent="Codex",
        to_agent="Codex",
        message_id="B6-msg-codex-preflight",
    )

    result = run_once(config=config, project_root=tmp_path, agent="Codex")

    assert result.status == "adapter_preflight_required"
    assert result.manifest_path is not None
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["adapter_type"] == "codex_cli"
    assert manifest["policy_snapshot"]["allow_modify_codex_automations"] is False
    assert manifest["codex"]["desktop_automation_allowed"] is False
    assert manifest["codex"]["automation_modification_policy"] == "forbidden_without_explicit_user_authorization"
    assert "modify_codex_desktop_automations" in manifest["forbidden_side_effects"]
    assert not (tmp_path / "docs" / "ai-workgroup" / "state" / "codex-automations").exists()


def test_real_write_adapter_still_requires_scope_gate_before_manifest(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path, agent="Claude-Code", allow_write=True)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(
        tmp_path,
        agent="Claude-Code",
        to_agent="Claude-Code",
        message_id="B6-msg-write-no-git",
        can_write=True,
        allowed_files=["src/**"],
    )

    result = run_once(config=config, project_root=tmp_path, agent="Claude-Code")

    assert result.status == "scope_denied"
    assert result.manifest_path is None
    assert "scope_gate_requires_git_worktree" in (result.error or "")
    assert db_rows(db_path, "SELECT id, status, attempt FROM tasks") == [
        ("B6-msg-write-no-git", "waiting_human", 0)
    ]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM events WHERE type = 'adapter_preflight_required'") == [(0,)]


def test_cli_run_once_reports_real_adapter_preflight_manifest_without_dispatch(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path, agent="OpenCode")
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(
        tmp_path,
        agent="OpenCode",
        to_agent="OpenCode",
        message_id="B6-msg-cli-preflight",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "OpenCode"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json", "--recent-events", "10"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert completed.returncode == 0, completed.stderr
    assert "status=adapter_preflight_required" in completed.stdout
    assert "message_id=B6-msg-cli-preflight" in completed.stdout
    assert "manifest=" in completed.stdout
    assert status_completed.returncode == 0, status_completed.stderr
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"waiting_human": 1}
    assert snapshot["tasks"][0]["id"] == "B6-msg-cli-preflight"
    assert snapshot["tasks"][0]["attempt"] == 0
    assert snapshot["agent_runs"] == []
    assert snapshot["recent_events"][0]["type"] == "adapter_preflight_required"

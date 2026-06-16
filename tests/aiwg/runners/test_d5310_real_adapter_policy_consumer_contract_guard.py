from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config
from aiwg.real_adapter_executor import execute_real_adapter_dry_run
from aiwg.real_adapter_process import run_supervised_sandbox_probe
from aiwg.real_adapter_sandbox import prepare_sandbox_invocation_plan
from aiwg.state.database import connect_database, init_database, utc_now_iso

SECRET_VALUE = "d5310-secret-token-should-never-appear"


def build_real_adapter_config(tmp_path: Path, *, allow_process: bool = False) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "real_adapter_execution_mode": "dry_run",
            "adapter_output_handoff": False,
            "allow_real_process_execution": allow_process,
            "allow_write": False,
            "allow_secret_access": False,
            "allow_network_write": False,
            "allow_destructive_commands": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["agents"]["OpenCode"]["enabled"] = True
    config["real_adapter_env"] = {
        "OPENAI_API_KEY": SECRET_VALUE,
        "AIWG_SANDBOX_HINT": "safe-non-secret-hint",
    }
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": ["OPENAI_API_KEY", "AIWG_SANDBOX_HINT"],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('d5310-probe-ok')"],
    }
    return config


def task(message_id: str = "D5310-msg") -> dict[str, Any]:
    return {
        "id": message_id,
        "task_id": message_id,
        "message_path": f"docs/ai-workgroup/inbox/OpenCode/{message_id}.md",
        "status": "waiting_human",
        "timeout_minutes": 30,
        "acceptance": [],
    }


def manifest_fixture(tmp_path: Path, *, message_id: str = "D5310-msg") -> tuple[dict[str, Any], Path, str]:
    artifact_dir = tmp_path / "docs" / "ai-workgroup" / "working" / "OpenCode" / message_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = artifact_dir / "prompt.md"
    prompt_path.write_text("# D5.3.10 harmless prompt fixture\n", encoding="utf-8")
    manifest_path = artifact_dir / "manifest.json"
    manifest = {
        "schema_version": "aiwg.adapter_preflight_manifest.v1",
        "artifacts": {"prompt_path": str(prompt_path)},
        "forbidden_side_effects": [
            "no_real_agent_binary",
            "no_secret_recording",
            "no_network_write",
            "no_protected_repo_write",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest, manifest_path, "sha-d5310-placeholder"


def seed_task_row(db_path: Path, *, message_id: str = "D5310-msg") -> None:
    now = utc_now_iso()
    current_task = task(message_id)
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks(
                id, task_id, message_path, from_agent, to_agent, type, status, priority,
                requires_human, can_write, worktree_required, max_scope, review_delegate,
                allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
                attempt, max_attempts, timeout_minutes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_task["id"],
                current_task["task_id"],
                current_task["message_path"],
                "CodeX",
                "OpenCode",
                "instruction",
                current_task["status"],
                "medium",
                0,
                0,
                0,
                "limited",
                "CodeX",
                "[]",
                "[]",
                "[]",
                json.dumps(current_task["acceptance"]),
                current_task.get("attempt", 0),
                2,
                current_task["timeout_minutes"],
                now,
                now,
            ),
        )


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def event_types(db_path: Path, message_id: str = "D5310-msg") -> list[str]:
    return [
        row[0]
        for row in db_rows(
            db_path,
            "SELECT type FROM events WHERE message_id = ? ORDER BY id",
            (message_id,),
        )
    ]


def latest_payload(db_path: Path, event_type: str, *, message_id: str = "D5310-msg") -> dict[str, Any]:
    row = db_rows(
        db_path,
        """
        SELECT payload_json
        FROM events
        WHERE message_id = ? AND type = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (message_id, event_type),
    )
    assert row
    return json.loads(row[0][0])


def assert_no_agent_runs(db_path: Path) -> None:
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]


def assert_config_contract_invalid(reason: str | None) -> None:
    assert reason is not None
    assert reason.startswith("config_contract_invalid:"), reason


@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, [], {}])
def test_d5310_executor_rejects_non_literal_adapter_output_handoff_before_handoff(
    tmp_path: Path,
    bad_value: Any,
) -> None:
    config = build_real_adapter_config(tmp_path)
    config["policy"]["adapter_output_handoff"] = bad_value
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = execute_real_adapter_dry_run(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "dry_run_policy_denied"
    stdout = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    assert stdout["adapter_result"]["handoff_allowed"] is False
    assert_config_contract_invalid(stdout["config_contract_errors"][0])
    assert "adapter_output_parsed" not in event_types(db_path)
    assert_no_agent_runs(db_path)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("allow_secret_access", "false"),
        ("allow_secret_access", 0),
        ("allow_network_write", "false"),
        ("allow_network_write", []),
    ],
)
def test_d5310_executor_environment_policy_requires_literal_bools(
    tmp_path: Path,
    key: str,
    bad_value: Any,
) -> None:
    config = build_real_adapter_config(tmp_path)
    config["policy"][key] = bad_value
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = execute_real_adapter_dry_run(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "dry_run_policy_denied"
    stdout = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    assert stdout["environment"]["secret_access_allowed"] is False
    assert stdout["environment"]["network_write_allowed"] is False
    assert_config_contract_invalid(stdout["config_contract_errors"][0])
    assert_no_agent_runs(db_path)


def test_d5310_executor_requires_network_write_policy_key_when_environment_contract_is_rendered(
    tmp_path: Path,
) -> None:
    config = build_real_adapter_config(tmp_path)
    del config["policy"]["allow_network_write"]
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = execute_real_adapter_dry_run(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "dry_run_policy_denied"
    stdout = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    assert stdout["environment"]["network_write_allowed"] is False
    assert_config_contract_invalid(stdout["config_contract_errors"][0])
    assert_no_agent_runs(db_path)


@pytest.mark.parametrize("bad_policy", [None, [], ["not", "mapping"]])
def test_d5310_sandbox_blocks_non_mapping_policy_before_plan_ready(tmp_path: Path, bad_policy: Any) -> None:
    config = build_real_adapter_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    config["policy"] = bad_policy
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = prepare_sandbox_invocation_plan(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "sandbox_invocation_blocked"
    assert result.plan_path is None
    assert_config_contract_invalid(result.error)
    assert "real_adapter_sandbox_invocation_ready" not in event_types(db_path)
    payload = latest_payload(db_path, "real_adapter_sandbox_invocation_blocked")
    assert payload["started_real_process"] is False
    assert payload["execution_authorized"] is False
    assert_config_contract_invalid(payload["reason"])
    assert_no_agent_runs(db_path)


@pytest.mark.parametrize("bad_value", ["false", 0, 1, [], {}])
def test_d5310_sandbox_secret_access_policy_requires_literal_bool(tmp_path: Path, bad_value: Any) -> None:
    config = build_real_adapter_config(tmp_path)
    config["policy"]["allow_secret_access"] = bad_value
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = prepare_sandbox_invocation_plan(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "sandbox_invocation_blocked"
    assert result.plan_path is None
    assert_config_contract_invalid(result.error)
    assert "real_adapter_sandbox_invocation_ready" not in event_types(db_path)
    assert_no_agent_runs(db_path)


@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, [], {}])
def test_d5310_process_execution_policy_requires_literal_bool_before_process_start(
    tmp_path: Path,
    bad_value: Any,
) -> None:
    config = build_real_adapter_config(tmp_path, allow_process=True)
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    config["policy"]["allow_real_process_execution"] = bad_value
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = run_supervised_sandbox_probe(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "sandbox_process_blocked"
    assert result.run_id is None
    assert_config_contract_invalid(result.error)
    assert_no_agent_runs(db_path)
    payload = latest_payload(db_path, "real_adapter_sandbox_process_blocked")
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
    assert_config_contract_invalid(payload["reason"])


def test_d5310_process_requires_network_write_policy_key_before_process_start(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path, allow_process=True)
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    del config["policy"]["allow_network_write"]
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = run_supervised_sandbox_probe(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "sandbox_process_blocked"
    assert result.run_id is None
    assert_config_contract_invalid(result.error)
    assert_no_agent_runs(db_path)
    payload = latest_payload(db_path, "real_adapter_sandbox_process_blocked")
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
    assert_config_contract_invalid(payload["reason"])


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("allow_secret_access", "false"),
        ("allow_secret_access", 0),
        ("allow_network_write", "false"),
        ("allow_network_write", []),
    ],
)
def test_d5310_process_environment_policy_blocks_before_process_start(
    tmp_path: Path,
    key: str,
    bad_value: Any,
) -> None:
    config = build_real_adapter_config(tmp_path, allow_process=True)
    db_path = init_database(config=config, project_root=tmp_path)
    seed_task_row(db_path)
    config["policy"][key] = bad_value
    manifest, manifest_path, manifest_sha256 = manifest_fixture(tmp_path)

    result = run_supervised_sandbox_probe(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task(),
        agent="OpenCode",
        adapter_type="opencode",
        approval_id="approval-d5310",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )

    assert result.status == "sandbox_process_blocked"
    assert result.run_id is None
    assert_config_contract_invalid(result.error)
    assert_no_agent_runs(db_path)
    payload = latest_payload(db_path, "real_adapter_sandbox_process_blocked")
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
    assert_config_contract_invalid(payload["reason"])

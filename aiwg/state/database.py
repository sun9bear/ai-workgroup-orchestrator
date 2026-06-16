from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiwg.evidence_paths import paths_overlap, protected_target_roots_from_config

SCHEMA_VERSION = 10
MIGRATIONS = (
    (1, "phase_a2_initial_schema"),
    (2, "phase_b0_schema_hardening"),
    (3, "phase_b7_operator_preflight_approval"),
    (4, "phase_d3_workflow_preflight"),
    (5, "phase_d4_git_steward_dry_run"),
    (6, "phase_d4_role_health_contract"),
    (7, "phase_d4_external_review_gate"),
    (8, "phase_d5_preflight_minimal"),
    (9, "phase_d5_1_preflight_controls"),
    (10, "phase_d5_2_preflight_hardening"),
)
MIGRATION_NAME = "phase_d5_2_preflight_hardening"
DEFAULT_BUSY_TIMEOUT_MS = 5000

TASK_STATUSES = (
    "ready",
    "claimed",
    "working",
    "reported",
    "reviewing",
    "needs_revision",
    "needs_review",
    "needs_clarification",
    "waiting_human",
    "waiting_codex",
    "review_degraded",
    "stale_claim",
    "needs_manual_recovery",
    "approved",
    "done",
    "cancelled",
    "failed",
    "archived",
)
TASK_TYPES = (
    "instruction",
    "report",
    "review",
    "decision",
    "blocker",
    "ack",
    "completion-report",
    "advisory",
    "advisory_report",
)
TASK_PRIORITIES = ("high", "medium", "low", "normal")
EXTERNAL_REVIEW_GATE_STATUSES = (
    "not_polled",
    "no_pr",
    "pending_review",
    "approved",
    "changes_requested",
    "blocked",
    "ci_failed",
    "stale",
    "unknown",
)
EXTERNAL_REVIEW_SOURCE_TYPES = (
    "github_pr",
    "codex_report",
    "reviewer_report",
    "human_report",
    "ci",
    "coderabbit",
    "security_scanner",
    "other",
)
EXTERNAL_REVIEW_FEEDBACK_CATEGORIES = (
    "must_fix",
    "should_fix",
    "question",
    "non_blocking",
    "human_gate",
    "out_of_scope",
)
EXTERNAL_REVIEW_ITEM_STATES = ("open", "resolved", "dismissed", "stale")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_project_root(
    config: dict[str, Any],
    *,
    config_path: Path | str | None = None,
    project_root: Path | str | None = None,
) -> Path:
    if project_root is not None:
        return Path(project_root).resolve()

    configured = Path(str(config.get("project_root") or "."))
    if configured.is_absolute():
        return configured.resolve()

    base = Path(config_path).resolve().parent if config_path is not None else Path.cwd()
    return (base / configured).resolve()


def resolve_config_path(config: dict[str, Any], key: str, project_root: Path | str) -> Path:
    value = Path(str(config[key]))
    if value.is_absolute():
        return value
    return Path(project_root) / value


def resolve_db_path(config: dict[str, Any], project_root: Path | str) -> Path:
    db_path = resolve_config_path(config, "state_db", project_root)
    for target_root in protected_target_roots_from_config(config):
        if paths_overlap(db_path, target_root):
            raise ValueError("state_db_overlaps_target_root")
    return db_path


def connect_database(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_database(config: dict[str, Any], project_root: Path | str) -> Path:
    db_path = resolve_db_path(config, project_root)
    with connect_database(db_path) as conn:
        _apply_schema(conn)
        _upsert_agent_capabilities(conn, config)
    return db_path


def _apply_schema(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        );

        {_tasks_table_sql("tasks")};

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT,
          message_id TEXT,
          agent TEXT NOT NULL,
          type TEXT NOT NULL,
          status TEXT,
          path TEXT,
          command TEXT,
          exit_code INTEGER,
          duration_ms INTEGER,
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS agent_runs (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          agent TEXT NOT NULL,
          adapter_type TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          timeout_seconds INTEGER,
          max_budget_usd REAL,
          prompt_path TEXT,
          stdout_path TEXT,
          stderr_path TEXT,
          report_path TEXT,
          exit_code INTEGER,
          error TEXT,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS verification_runs (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          command TEXT NOT NULL,
          cwd TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          duration_ms INTEGER,
          exit_code INTEGER,
          stdout_path TEXT,
          stderr_path TEXT,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS agent_capabilities (
          agent TEXT PRIMARY KEY,
          adapter_type TEXT NOT NULL,
          can_read INTEGER NOT NULL DEFAULT 1 CHECK(can_read IN (0, 1)),
          can_write INTEGER NOT NULL DEFAULT 0 CHECK(can_write IN (0, 1)),
          can_run_shell INTEGER NOT NULL DEFAULT 0 CHECK(can_run_shell IN (0, 1)),
          can_review INTEGER NOT NULL DEFAULT 0 CHECK(can_review IN (0, 1)),
          can_plan INTEGER NOT NULL DEFAULT 0 CHECK(can_plan IN (0, 1)),
          default_timeout_minutes INTEGER NOT NULL DEFAULT 30 CHECK(default_timeout_minutes > 0),
          daily_limit INTEGER CHECK(daily_limit IS NULL OR daily_limit >= 0),
          max_budget_usd REAL CHECK(max_budget_usd IS NULL OR max_budget_usd >= 0),
          config_json TEXT NOT NULL DEFAULT '{{}}',
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS git_refs (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          task_id TEXT NOT NULL,
          branch TEXT,
          worktree_path TEXT,
          base_branch TEXT,
          base_sha TEXT,
          head_sha TEXT,
          commit_proposed INTEGER NOT NULL DEFAULT 0 CHECK(commit_proposed IN (0, 1)),
          commit_sha TEXT,
          pr_proposed INTEGER NOT NULL DEFAULT 0 CHECK(pr_proposed IN (0, 1)),
          pr_url TEXT,
          ci_status TEXT,
          merge_ready INTEGER NOT NULL DEFAULT 0 CHECK(merge_ready IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS operator_approvals (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          agent TEXT NOT NULL,
          adapter_type TEXT NOT NULL,
          manifest_path TEXT NOT NULL,
          manifest_sha256 TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
          operator TEXT NOT NULL,
          reason TEXT,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          used_at TEXT,
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS workflow_runs (
          workflow_id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run IN (0, 1)),
          idempotency_key TEXT,
          last_successful_step_id TEXT,
          real_agents_started INTEGER NOT NULL DEFAULT 0 CHECK(real_agents_started IN (0, 1)),
          target_writes_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_writes_performed IN (0, 1)),
          mcp_mutation_tools_exposed INTEGER NOT NULL DEFAULT 0 CHECK(mcp_mutation_tools_exposed IN (0, 1)),
          artifact_root TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflow_steps (
          workflow_id TEXT NOT NULL,
          step_id TEXT NOT NULL,
          position INTEGER NOT NULL CHECK(position >= 0),
          adapter_type TEXT NOT NULL,
          status TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          target_root TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(workflow_id, step_id),
          FOREIGN KEY(workflow_id) REFERENCES workflow_runs(workflow_id)
        );

        CREATE TABLE IF NOT EXISTS workflow_step_intents (
          id TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL,
          step_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(workflow_id, step_id) REFERENCES workflow_steps(workflow_id, step_id)
        );

        CREATE TABLE IF NOT EXISTS workflow_step_outputs (
          id TEXT PRIMARY KEY,
          intent_id TEXT NOT NULL,
          workflow_id TEXT NOT NULL,
          step_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          status TEXT NOT NULL,
          artifact_path TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(intent_id) REFERENCES workflow_step_intents(id),
          FOREIGN KEY(workflow_id, step_id) REFERENCES workflow_steps(workflow_id, step_id)
        );

        CREATE TABLE IF NOT EXISTS git_worktree_proposals (
          plan_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          target_root TEXT NOT NULL,
          scope_key TEXT NOT NULL,
          base_branch TEXT NOT NULL,
          branch_name TEXT NOT NULL,
          worktree_path TEXT NOT NULL,
          status TEXT NOT NULL,
          dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run IN (0, 1)),
          target_writes_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_writes_performed IN (0, 1)),
          git_push_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_push_performed IN (0, 1)),
          git_merge_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_merge_performed IN (0, 1)),
          mcp_mutation_tools_exposed INTEGER NOT NULL DEFAULT 0 CHECK(mcp_mutation_tools_exposed IN (0, 1)),
          included_files_json TEXT NOT NULL DEFAULT '[]',
          excluded_files_json TEXT NOT NULL DEFAULT '[]',
          artifact_path TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS git_commit_proposals (
          plan_id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          scope_key TEXT NOT NULL,
          branch_name TEXT NOT NULL,
          status TEXT NOT NULL,
          commit_message TEXT NOT NULL,
          included_files_json TEXT NOT NULL DEFAULT '[]',
          excluded_files_json TEXT NOT NULL DEFAULT '[]',
          dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run IN (0, 1)),
          commit_performed INTEGER NOT NULL DEFAULT 0 CHECK(commit_performed IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pr_gate_status (
          plan_id TEXT PRIMARY KEY,
          gate_state TEXT NOT NULL,
          required_checks_state TEXT NOT NULL DEFAULT 'not_polled',
          review_threads_state TEXT NOT NULL DEFAULT 'not_polled',
          review_decision TEXT,
          pr_url TEXT,
          read_only INTEGER NOT NULL DEFAULT 1 CHECK(read_only IN (0, 1)),
          mutation_actions_json TEXT NOT NULL DEFAULT '[]',
          pr_mutation_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_mutation_performed IN (0, 1)),
          merge_performed INTEGER NOT NULL DEFAULT 0 CHECK(merge_performed IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_states (
          role TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          adapter_type TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
          health_status TEXT NOT NULL CHECK(health_status IN (
            'healthy', 'idle', 'stale', 'blocked', 'failed', 'unknown',
            'disabled', 'waiting_human', 'waiting_peer', 'queue_empty'
          )),
          health_reason TEXT CHECK(health_reason IS NULL OR health_reason IN (
            'no_recent_heartbeat', 'ready_task_unconsumed', 'claimed_task_stale',
            'failed_task_present', 'human_gate_present', 'runner_disabled',
            'scheduler_disabled', 'queue_empty', 'reviewer_pending',
            'git_steward_pending', 'recent_heartbeat'
          )),
          last_seen_at TEXT,
          current_task_id TEXT,
          detail_json TEXT NOT NULL DEFAULT '{{}}',
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_health_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          role TEXT NOT NULL,
          event_type TEXT NOT NULL,
          health_status TEXT NOT NULL CHECK(health_status IN (
            'healthy', 'idle', 'stale', 'blocked', 'failed', 'unknown',
            'disabled', 'waiting_human', 'waiting_peer', 'queue_empty'
          )),
          health_reason TEXT CHECK(health_reason IS NULL OR health_reason IN (
            'no_recent_heartbeat', 'ready_task_unconsumed', 'claimed_task_stale',
            'failed_task_present', 'human_gate_present', 'runner_disabled',
            'scheduler_disabled', 'queue_empty', 'reviewer_pending',
            'git_steward_pending', 'recent_heartbeat'
          )),
          task_id TEXT,
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS external_review_sources (
          id TEXT PRIMARY KEY,
          source_type TEXT NOT NULL CHECK(source_type IN ({_sql_literal_list(EXTERNAL_REVIEW_SOURCE_TYPES)})),
          display_name TEXT NOT NULL,
          provider_ref TEXT,
          gate_state TEXT NOT NULL CHECK(gate_state IN ({_sql_literal_list(EXTERNAL_REVIEW_GATE_STATUSES)})),
          last_polled_at TEXT,
          read_only INTEGER NOT NULL DEFAULT 1 CHECK(read_only IN (0, 1)),
          mutation_actions_json TEXT NOT NULL DEFAULT '[]',
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS external_review_items (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          source_type TEXT NOT NULL CHECK(source_type IN ({_sql_literal_list(EXTERNAL_REVIEW_SOURCE_TYPES)})),
          item_state TEXT NOT NULL DEFAULT 'open' CHECK(item_state IN ({_sql_literal_list(EXTERNAL_REVIEW_ITEM_STATES)})),
          feedback_category TEXT NOT NULL CHECK(feedback_category IN ({_sql_literal_list(EXTERNAL_REVIEW_FEEDBACK_CATEGORIES)})),
          title TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          file_path TEXT,
          line INTEGER CHECK(line IS NULL OR line >= 1),
          resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0, 1)),
          blocking INTEGER NOT NULL DEFAULT 0 CHECK(blocking IN (0, 1)),
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(source_id) REFERENCES external_review_sources(id)
        );

        CREATE TABLE IF NOT EXISTS external_review_gate_snapshots (
          id TEXT PRIMARY KEY,
          gate_state TEXT NOT NULL CHECK(gate_state IN ({_sql_literal_list(EXTERNAL_REVIEW_GATE_STATUSES)})),
          source_count INTEGER NOT NULL DEFAULT 0 CHECK(source_count >= 0),
          item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count >= 0),
          unresolved_actionable_count INTEGER NOT NULL DEFAULT 0 CHECK(unresolved_actionable_count >= 0),
          summary_json TEXT NOT NULL DEFAULT '{{}}',
          read_only INTEGER NOT NULL DEFAULT 1 CHECK(read_only IN (0, 1)),
          mutation_actions_json TEXT NOT NULL DEFAULT '[]',
          git_push_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_push_performed IN (0, 1)),
          git_merge_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_merge_performed IN (0, 1)),
          pr_comment_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_comment_performed IN (0, 1)),
          target_writes_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_writes_performed IN (0, 1)),
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS d5_preflight_runs (
          id TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('passed_dry_run', 'blocked', 'failed')),
          dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run = 1),
          fake_only INTEGER NOT NULL DEFAULT 1 CHECK(fake_only = 1),
          ready_for_real_agent_execution INTEGER NOT NULL DEFAULT 0 CHECK(ready_for_real_agent_execution = 0),
          ready_for_protected_business_repository_write INTEGER NOT NULL DEFAULT 0 CHECK(ready_for_protected_business_repository_write = 0),
          target_writes_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_writes_performed = 0),
          mcp_mutation_tools_exposed INTEGER NOT NULL DEFAULT 0 CHECK(mcp_mutation_tools_exposed = 0),
          github_write_api_called INTEGER NOT NULL DEFAULT 0 CHECK(github_write_api_called = 0),
          pr_comment_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_comment_performed = 0),
          pr_mutation_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_mutation_performed = 0),
          created_fix_tasks INTEGER NOT NULL DEFAULT 0 CHECK(created_fix_tasks = 0),
          codex_automation_modified INTEGER NOT NULL DEFAULT 0 CHECK(codex_automation_modified = 0),
          git_push_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_push_performed = 0),
          git_merge_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_merge_performed = 0),
          git_deploy_performed INTEGER NOT NULL DEFAULT 0 CHECK(git_deploy_performed = 0),
          real_agents_started INTEGER NOT NULL DEFAULT 0 CHECK(real_agents_started = 0),
          real_processes_started INTEGER NOT NULL DEFAULT 0 CHECK(real_processes_started = 0),
          artifact_path TEXT,
          artifact_sha256 TEXT,
          policy_denials_json TEXT NOT NULL DEFAULT '[]',
          deferred_to_d5_1_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS d5_artifact_provenance (
          id TEXT PRIMARY KEY,
          preflight_run_id TEXT NOT NULL,
          artifact_kind TEXT NOT NULL,
          artifact_path TEXT NOT NULL,
          artifact_sha256 TEXT NOT NULL,
          origin_component TEXT NOT NULL,
          workflow_id TEXT,
          step_id TEXT,
          intent_id TEXT,
          under_orchestrator_root INTEGER NOT NULL CHECK(under_orchestrator_root = 1),
          under_target_root INTEGER NOT NULL CHECK(under_target_root = 0),
          created_at TEXT NOT NULL,
          UNIQUE(preflight_run_id, artifact_path),
          FOREIGN KEY(preflight_run_id) REFERENCES d5_preflight_runs(id)
        );

        CREATE TABLE IF NOT EXISTS d5_budget_preflight (
          id TEXT PRIMARY KEY,
          preflight_run_id TEXT NOT NULL,
          role TEXT NOT NULL,
          workflow_id TEXT NOT NULL,
          max_budget_usd REAL NOT NULL DEFAULT 0 CHECK(max_budget_usd >= 0),
          requested_budget_usd REAL NOT NULL DEFAULT 0 CHECK(requested_budget_usd >= 0),
          consumed_budget_usd REAL NOT NULL DEFAULT 0 CHECK(consumed_budget_usd = 0),
          status TEXT NOT NULL CHECK(status IN ('within_budget', 'budget_exceeded', 'blocked')),
          dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run = 1),
          created_at TEXT NOT NULL,
          FOREIGN KEY(preflight_run_id) REFERENCES d5_preflight_runs(id)
        );

        CREATE TABLE IF NOT EXISTS d5_checkpoint_lease_preflight (
          id TEXT PRIMARY KEY,
          preflight_run_id TEXT NOT NULL,
          workflow_id TEXT NOT NULL,
          checkpoint_id TEXT NOT NULL,
          role TEXT NOT NULL,
          lease_state TEXT NOT NULL CHECK(lease_state IN (
            'would_acquire', 'would_wait', 'would_mark_stale_requires_human', 'would_skip', 'checked'
          )),
          real_lock_acquired INTEGER NOT NULL DEFAULT 0 CHECK(real_lock_acquired = 0),
          stale_recovery_performed INTEGER NOT NULL DEFAULT 0 CHECK(stale_recovery_performed = 0),
          reset_to_ready_performed INTEGER NOT NULL DEFAULT 0 CHECK(reset_to_ready_performed = 0),
          heartbeat_expected_seconds INTEGER NOT NULL CHECK(heartbeat_expected_seconds > 0),
          stale_after_seconds INTEGER NOT NULL CHECK(stale_after_seconds > 0),
          created_at TEXT NOT NULL,
          FOREIGN KEY(preflight_run_id) REFERENCES d5_preflight_runs(id)
        );

        {_d5_external_review_fixture_ingest_table_sql("d5_external_review_fixture_ingest")}
        """
    )
    if _tasks_schema_needs_hardening(conn):
        _rebuild_tasks_table_with_hardened_schema(conn)
    if _schema_references_tasks_old(conn) or _table_exists(conn, "tasks_old"):
        _repair_task_foreign_key_tables(conn)
    if _d5_external_review_fixture_ingest_needs_hardening(conn):
        _rebuild_d5_external_review_fixture_ingest_table(conn)
    _create_indexes(conn)
    for version, name in MIGRATIONS:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, now),
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _sql_literal_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _d5_external_review_fixture_ingest_table_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id TEXT PRIMARY KEY,
          preflight_run_id TEXT NOT NULL,
          fixture_path TEXT NOT NULL,
          fixture_sha256 TEXT NOT NULL,
          source_count INTEGER NOT NULL DEFAULT 0 CHECK(source_count >= 0),
          item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count >= 0),
          status TEXT NOT NULL CHECK(status IN ('ingested_read_only', 'blocked', 'not_provided')),
          gate_state TEXT NOT NULL CHECK(gate_state IN ({_sql_literal_list(EXTERNAL_REVIEW_GATE_STATUSES)})),
          read_only INTEGER NOT NULL DEFAULT 1 CHECK(read_only = 1),
          fixture_declared_read_only INTEGER NOT NULL DEFAULT 1 CHECK(fixture_declared_read_only IN (0, 1)),
          mutation_action_count INTEGER NOT NULL DEFAULT 0 CHECK(mutation_action_count >= 0),
          mutation_actions_json TEXT NOT NULL DEFAULT '[]',
          github_write_api_called INTEGER NOT NULL DEFAULT 0 CHECK(github_write_api_called = 0),
          pr_comment_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_comment_performed = 0),
          pr_mutation_performed INTEGER NOT NULL DEFAULT 0 CHECK(pr_mutation_performed = 0),
          created_fix_tasks INTEGER NOT NULL DEFAULT 0 CHECK(created_fix_tasks = 0),
          target_writes_performed INTEGER NOT NULL DEFAULT 0 CHECK(target_writes_performed = 0),
          codex_automation_modified INTEGER NOT NULL DEFAULT 0 CHECK(codex_automation_modified = 0),
          created_at TEXT NOT NULL,
          CHECK(fixture_declared_read_only = 1 OR status = 'blocked'),
          CHECK(mutation_action_count = 0 OR status = 'blocked'),
          FOREIGN KEY(preflight_run_id) REFERENCES d5_preflight_runs(id)
        );
        """


def _tasks_table_sql(table_name: str) -> str:
    statuses = _sql_literal_list(TASK_STATUSES)
    task_types = _sql_literal_list(TASK_TYPES)
    priorities = _sql_literal_list(TASK_PRIORITIES)
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL,
          message_path TEXT NOT NULL UNIQUE,
          from_agent TEXT NOT NULL,
          to_agent TEXT NOT NULL,
          type TEXT NOT NULL CHECK(type IN ({task_types})),
          status TEXT NOT NULL CHECK(status IN ({statuses})),
          priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ({priorities})),
          requires_human INTEGER NOT NULL DEFAULT 0 CHECK(requires_human IN (0, 1)),
          can_write INTEGER NOT NULL DEFAULT 0 CHECK(can_write IN (0, 1)),
          worktree_required INTEGER NOT NULL DEFAULT 0 CHECK(worktree_required IN (0, 1)),
          max_scope TEXT NOT NULL DEFAULT 'limited',
          review_delegate TEXT,
          allowed_files_json TEXT NOT NULL DEFAULT '[]',
          forbidden_files_json TEXT NOT NULL DEFAULT '[]',
          context_files_json TEXT NOT NULL DEFAULT '[]',
          acceptance_json TEXT NOT NULL DEFAULT '[]',
          claimed_by TEXT,
          claimed_at TEXT,
          lock_id TEXT,
          attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
          max_attempts INTEGER NOT NULL DEFAULT 2 CHECK(max_attempts >= 1),
          timeout_minutes INTEGER NOT NULL DEFAULT 30 CHECK(timeout_minutes > 0),
          max_budget_usd REAL CHECK(max_budget_usd IS NULL OR max_budget_usd >= 0),
          content_hash TEXT,
          frontmatter_hash TEXT,
          legacy_imported INTEGER NOT NULL DEFAULT 0 CHECK(legacy_imported IN (0, 1)),
          legacy_source_path TEXT,
          git_branch TEXT,
          worktree_path TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        )
        """


def _tasks_schema_needs_hardening(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
    if row is None or row[0] is None:
        return False
    table_sql = str(row[0])
    if "CHECK(status IN" not in table_sql:
        return True
    unique_indexes = {
        index_row[1]
        for index_row in conn.execute("PRAGMA index_list(tasks)").fetchall()
        if int(index_row[2]) == 1
    }
    for index_name in unique_indexes:
        columns = [column_row[2] for column_row in conn.execute(f"PRAGMA index_info({index_name})").fetchall()]
        if columns == ["message_path"]:
            return False
    return True


def _rebuild_tasks_table_with_hardened_schema(conn: sqlite3.Connection) -> None:
    columns = [
        "id",
        "task_id",
        "message_path",
        "from_agent",
        "to_agent",
        "type",
        "status",
        "priority",
        "requires_human",
        "can_write",
        "worktree_required",
        "max_scope",
        "review_delegate",
        "allowed_files_json",
        "forbidden_files_json",
        "context_files_json",
        "acceptance_json",
        "claimed_by",
        "claimed_at",
        "lock_id",
        "attempt",
        "max_attempts",
        "timeout_minutes",
        "max_budget_usd",
        "content_hash",
        "frontmatter_hash",
        "legacy_imported",
        "legacy_source_path",
        "git_branch",
        "worktree_path",
        "created_at",
        "updated_at",
        "completed_at",
    ]
    column_csv = ", ".join(columns)
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE tasks RENAME TO tasks_old")
        conn.execute(_tasks_table_sql("tasks"))
        conn.execute(f"INSERT INTO tasks({column_csv}) SELECT {column_csv} FROM tasks_old")
        conn.execute("DROP TABLE tasks_old")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.commit()
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _d5_external_review_fixture_ingest_needs_hardening(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "d5_external_review_fixture_ingest"):
        return False
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='d5_external_review_fixture_ingest'"
    ).fetchone()
    table_sql = str(row[0] or "") if row is not None else ""
    return (
        "fixture_declared_read_only" not in table_sql
        or "mutation_action_count = 0 OR status = 'blocked'" not in table_sql
    )


def _rebuild_d5_external_review_fixture_ingest_table(conn: sqlite3.Connection) -> None:
    table_name = "d5_external_review_fixture_ingest"
    backup_name = f"{table_name}_old_d52"
    columns = _table_columns(conn, table_name)
    declared_expr = (
        "fixture_declared_read_only"
        if "fixture_declared_read_only" in columns
        else "CASE WHEN status = 'blocked' AND mutation_action_count > 0 THEN 0 ELSE read_only END"
    )
    status_expr = f"CASE WHEN mutation_action_count > 0 OR ({declared_expr}) = 0 THEN 'blocked' ELSE status END"
    gate_state_expr = f"CASE WHEN mutation_action_count > 0 OR ({declared_expr}) = 0 THEN 'blocked' ELSE gate_state END"
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(f"DROP TABLE IF EXISTS {backup_name}")
        conn.execute(f"ALTER TABLE {table_name} RENAME TO {backup_name}")
        conn.execute(_d5_external_review_fixture_ingest_table_sql(table_name))
        conn.execute(
            f"""
            INSERT INTO {table_name}(
              id, preflight_run_id, fixture_path, fixture_sha256, source_count,
              item_count, status, gate_state, read_only, fixture_declared_read_only,
              mutation_action_count, mutation_actions_json, github_write_api_called,
              pr_comment_performed, pr_mutation_performed, created_fix_tasks,
              target_writes_performed, codex_automation_modified, created_at
            )
            SELECT
              id, preflight_run_id, fixture_path, fixture_sha256, source_count,
              item_count, {status_expr}, {gate_state_expr}, 1,
              CASE WHEN ({declared_expr}) = 1 THEN 1 ELSE 0 END,
              mutation_action_count, mutation_actions_json, github_write_api_called,
              pr_comment_performed, pr_mutation_performed, created_fix_tasks,
              target_writes_performed, codex_automation_modified, created_at
            FROM {backup_name}
            """
        )
        conn.execute(f"DROP TABLE {backup_name}")
    finally:
        conn.commit()
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")


def _schema_references_tasks_old(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type='table'
          AND name IN ('events', 'agent_runs', 'verification_runs', 'git_refs', 'operator_approvals')
        """
    ).fetchall()
    return any("tasks_old" in str(row[0]) for row in rows if row[0] is not None)


def _repair_task_foreign_key_tables(conn: sqlite3.Connection) -> None:
    """Repair an interrupted tasks-table rebuild that left FKs on tasks_old."""

    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        _rebuild_existing_table(
            conn,
            table_name="events",
            create_sql=_events_table_sql,
            columns=(
                "id",
                "task_id",
                "message_id",
                "agent",
                "type",
                "status",
                "path",
                "command",
                "exit_code",
                "duration_ms",
                "payload_json",
                "created_at",
            ),
        )
        _rebuild_existing_table(
            conn,
            table_name="agent_runs",
            create_sql=_agent_runs_table_sql,
            columns=(
                "id",
                "message_id",
                "agent",
                "adapter_type",
                "status",
                "started_at",
                "finished_at",
                "timeout_seconds",
                "max_budget_usd",
                "prompt_path",
                "stdout_path",
                "stderr_path",
                "report_path",
                "exit_code",
                "error",
            ),
        )
        _rebuild_existing_table(
            conn,
            table_name="verification_runs",
            create_sql=_verification_runs_table_sql,
            columns=(
                "id",
                "message_id",
                "command",
                "cwd",
                "status",
                "started_at",
                "finished_at",
                "duration_ms",
                "exit_code",
                "stdout_path",
                "stderr_path",
            ),
        )
        _rebuild_existing_table(
            conn,
            table_name="git_refs",
            create_sql=_git_refs_table_sql,
            columns=(
                "id",
                "message_id",
                "task_id",
                "branch",
                "worktree_path",
                "base_branch",
                "base_sha",
                "head_sha",
                "commit_proposed",
                "commit_sha",
                "pr_proposed",
                "pr_url",
                "ci_status",
                "merge_ready",
                "created_at",
                "updated_at",
            ),
        )
        _rebuild_existing_table(
            conn,
            table_name="operator_approvals",
            create_sql=_operator_approvals_table_sql,
            columns=(
                "id",
                "message_id",
                "agent",
                "adapter_type",
                "manifest_path",
                "manifest_sha256",
                "decision",
                "operator",
                "reason",
                "expires_at",
                "created_at",
                "used_at",
                "payload_json",
            ),
        )
        conn.execute("DROP TABLE IF EXISTS tasks_old")
    finally:
        conn.commit()
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")


def _rebuild_existing_table(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    create_sql,
    columns: tuple[str, ...],
) -> None:
    if not _table_exists(conn, table_name):
        return
    backup_name = f"{table_name}_old_fk"
    conn.execute(f"DROP TABLE IF EXISTS {backup_name}")
    conn.execute(f"ALTER TABLE {table_name} RENAME TO {backup_name}")
    conn.execute(create_sql(table_name))
    column_csv = ", ".join(columns)
    conn.execute(f"INSERT INTO {table_name}({column_csv}) SELECT {column_csv} FROM {backup_name}")
    conn.execute(f"DROP TABLE {backup_name}")


def _events_table_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT,
          message_id TEXT,
          agent TEXT NOT NULL,
          type TEXT NOT NULL,
          status TEXT,
          path TEXT,
          command TEXT,
          exit_code INTEGER,
          duration_ms INTEGER,
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        )
        """


def _agent_runs_table_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          agent TEXT NOT NULL,
          adapter_type TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          timeout_seconds INTEGER,
          max_budget_usd REAL,
          prompt_path TEXT,
          stdout_path TEXT,
          stderr_path TEXT,
          report_path TEXT,
          exit_code INTEGER,
          error TEXT,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        )
        """


def _verification_runs_table_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          command TEXT NOT NULL,
          cwd TEXT NOT NULL,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          duration_ms INTEGER,
          exit_code INTEGER,
          stdout_path TEXT,
          stderr_path TEXT,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        )
        """


def _git_refs_table_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          task_id TEXT NOT NULL,
          branch TEXT,
          worktree_path TEXT,
          base_branch TEXT,
          base_sha TEXT,
          head_sha TEXT,
          commit_proposed INTEGER NOT NULL DEFAULT 0 CHECK(commit_proposed IN (0, 1)),
          commit_sha TEXT,
          pr_proposed INTEGER NOT NULL DEFAULT 0 CHECK(pr_proposed IN (0, 1)),
          pr_url TEXT,
          ci_status TEXT,
          merge_ready INTEGER NOT NULL DEFAULT 0 CHECK(merge_ready IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        )
        """


def _operator_approvals_table_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
          id TEXT PRIMARY KEY,
          message_id TEXT NOT NULL,
          agent TEXT NOT NULL,
          adapter_type TEXT NOT NULL,
          manifest_path TEXT NOT NULL,
          manifest_sha256 TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
          operator TEXT NOT NULL,
          reason TEXT,
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL,
          used_at TEXT,
          payload_json TEXT NOT NULL DEFAULT '{{}}',
          FOREIGN KEY(message_id) REFERENCES tasks(id)
        )
        """


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_status_agent ON tasks(status, to_agent);
        CREATE INDEX IF NOT EXISTS idx_tasks_task_id ON tasks(task_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_message_path_unique ON tasks(message_path);
        CREATE INDEX IF NOT EXISTS idx_events_message_id ON events(message_id);
        CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_message_id ON agent_runs(message_id);
        CREATE INDEX IF NOT EXISTS idx_verification_runs_message_id ON verification_runs(message_id);
        CREATE INDEX IF NOT EXISTS idx_git_refs_message_id ON git_refs(message_id);
        CREATE INDEX IF NOT EXISTS idx_operator_approvals_message_id ON operator_approvals(message_id);
        CREATE INDEX IF NOT EXISTS idx_operator_approvals_agent_decision ON operator_approvals(agent, decision);
        CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow ON workflow_steps(workflow_id, position);
        CREATE INDEX IF NOT EXISTS idx_workflow_step_intents_workflow ON workflow_step_intents(workflow_id, step_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_step_outputs_succeeded_idempotency
          ON workflow_step_outputs(idempotency_key)
          WHERE status = 'succeeded';
        CREATE INDEX IF NOT EXISTS idx_git_worktree_proposals_status ON git_worktree_proposals(status, scope_key);
        CREATE INDEX IF NOT EXISTS idx_git_commit_proposals_scope ON git_commit_proposals(scope_key, status);
        CREATE INDEX IF NOT EXISTS idx_pr_gate_status_gate_state ON pr_gate_status(gate_state);
        CREATE INDEX IF NOT EXISTS idx_agent_states_health ON agent_states(health_status, health_reason);
        CREATE INDEX IF NOT EXISTS idx_agent_health_events_role_created ON agent_health_events(role, created_at);
        CREATE INDEX IF NOT EXISTS idx_external_review_sources_state ON external_review_sources(gate_state, source_type);
        CREATE INDEX IF NOT EXISTS idx_external_review_items_source_category
          ON external_review_items(source_id, feedback_category, resolved, blocking);
        CREATE INDEX IF NOT EXISTS idx_external_review_gate_snapshots_created
          ON external_review_gate_snapshots(created_at);
        """
    )


def _upsert_agent_capabilities(conn: sqlite3.Connection, config: dict[str, Any]) -> None:
    import json

    now = utc_now_iso()
    policy = config.get("policy") or {}
    default_timeout = int(policy.get("default_timeout_minutes") or 30)
    agents = config.get("agents") or {}
    for agent, agent_config in agents.items():
        if not isinstance(agent_config, dict):
            continue
        conn.execute(
            """
            INSERT INTO agent_capabilities(
              agent, adapter_type, can_read, can_write, can_run_shell, can_review,
              can_plan, default_timeout_minutes, config_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent) DO UPDATE SET
              adapter_type=excluded.adapter_type,
              can_write=excluded.can_write,
              default_timeout_minutes=excluded.default_timeout_minutes,
              config_json=excluded.config_json,
              updated_at=excluded.updated_at
            """,
            (
                str(agent),
                str(agent_config.get("adapter") or "unknown"),
                1,
                1 if bool(agent_config.get("can_write")) else 0,
                0,
                1 if str(agent) in {"CodeX", "Codex", "Hermes"} else 0,
                1 if str(agent) in {"CodeX", "Codex", "Hermes", "OpenCode"} else 0,
                default_timeout,
                json.dumps(agent_config, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

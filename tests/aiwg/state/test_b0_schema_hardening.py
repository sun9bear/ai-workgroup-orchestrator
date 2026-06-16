from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aiwg.config import build_default_config
from aiwg.state.database import connect_database, init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_test_config(tmp_path: Path) -> dict:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    return config


def insert_task(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    message_path: str | None = None,
    task_status: str = "ready",
    task_type: str = "instruction",
    priority: str = "medium",
    requires_human: int = 0,
    can_write: int = 0,
    worktree_required: int = 0,
    max_scope: str = "limited",
    attempt: int = 0,
    max_attempts: int = 2,
    timeout_minutes: int = 30,
    legacy_imported: int = 0,
) -> None:
    now = "2026-06-04T00:00:00Z"
    conn.execute(
        """
        INSERT INTO tasks(
          id, task_id, message_path, from_agent, to_agent, type, status, priority,
          requires_human, can_write, worktree_required, max_scope,
          allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
          attempt, max_attempts, timeout_minutes, legacy_imported, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            "B0-schema-hardening",
            message_path or f"docs/ai-workgroup/inbox/Fake/{message_id}.md",
            "CodeX",
            "Fake",
            task_type,
            task_status,
            priority,
            requires_human,
            can_write,
            worktree_required,
            max_scope,
            "[]",
            "[]",
            "[]",
            "[]",
            attempt,
            max_attempts,
            timeout_minutes,
            legacy_imported,
            now,
            now,
        ),
    )


def test_tasks_schema_rejects_invalid_state_and_bool_values(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-status", task_status="teleported")

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-type", task_type="random")

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-requires-human", requires_human=2)

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-can-write", can_write=2)

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-worktree-required", worktree_required=-1)

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-legacy-imported", legacy_imported=3)


def test_tasks_schema_rejects_invalid_attempt_timeout_and_duplicate_message_path(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-attempt", attempt=-1)

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-max-attempts", max_attempts=0)

        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="bad-timeout", timeout_minutes=0)

        insert_task(conn, message_id="path-one", message_path="docs/ai-workgroup/inbox/Fake/same.md")
        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="path-two", message_path="docs/ai-workgroup/inbox/Fake/same.md")


def test_init_database_migrates_existing_phase_a_schema_with_events_to_hardened_tasks(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations(version, name, applied_at)
            VALUES (1, 'phase_a2_initial_schema', '2026-06-04T00:00:00Z');

            CREATE TABLE tasks (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              message_path TEXT NOT NULL,
              from_agent TEXT NOT NULL,
              to_agent TEXT NOT NULL,
              type TEXT NOT NULL,
              status TEXT NOT NULL,
              priority TEXT NOT NULL DEFAULT 'normal',
              requires_human INTEGER NOT NULL DEFAULT 0,
              can_write INTEGER NOT NULL DEFAULT 0,
              worktree_required INTEGER NOT NULL DEFAULT 0,
              max_scope TEXT NOT NULL DEFAULT 'limited',
              review_delegate TEXT,
              allowed_files_json TEXT NOT NULL DEFAULT '[]',
              forbidden_files_json TEXT NOT NULL DEFAULT '[]',
              context_files_json TEXT NOT NULL DEFAULT '[]',
              acceptance_json TEXT NOT NULL DEFAULT '[]',
              claimed_by TEXT,
              claimed_at TEXT,
              lock_id TEXT,
              attempt INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 2,
              timeout_minutes INTEGER NOT NULL DEFAULT 30,
              max_budget_usd REAL,
              content_hash TEXT,
              frontmatter_hash TEXT,
              legacy_imported INTEGER NOT NULL DEFAULT 0,
              legacy_source_path TEXT,
              git_branch TEXT,
              worktree_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            );
            CREATE TABLE events (
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
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(message_id) REFERENCES tasks(id)
            );
            """
        )
        insert_task(conn, message_id="phase-a-existing", task_status="done")
        conn.execute(
            """
            INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "B0-schema-hardening",
                "phase-a-existing",
                "Fake",
                "task_done",
                "done",
                "docs/ai-workgroup/inbox/Fake/phase-a-existing.md",
                "{}",
                "2026-06-04T00:00:00Z",
            ),
        )

    migrated_db_path = init_database(config=config, project_root=tmp_path)

    assert migrated_db_path == db_path
    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
        ]
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", ("phase-a-existing",)).fetchone()[0] == "done"
        assert conn.execute("SELECT message_id FROM events").fetchall() == [("phase-a-existing",)]
        table_schemas = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        assert "tasks_old" not in {name for name, _sql in table_schemas}
        assert all("tasks_old" not in str(sql) for _name, sql in table_schemas)
        assert "REFERENCES tasks(id)" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()[0]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            insert_task(conn, message_id="invalid-after-migration", task_status="teleported")


def test_init_database_repairs_partial_failed_tasks_old_migration(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    with connect_database(db_path) as conn:
        insert_task(conn, message_id="partial-existing", task_status="done")
        conn.executescript(
            """
            CREATE TABLE tasks_old (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              message_path TEXT NOT NULL,
              from_agent TEXT NOT NULL,
              to_agent TEXT NOT NULL,
              type TEXT NOT NULL,
              status TEXT NOT NULL,
              priority TEXT NOT NULL DEFAULT 'normal',
              requires_human INTEGER NOT NULL DEFAULT 0,
              can_write INTEGER NOT NULL DEFAULT 0,
              worktree_required INTEGER NOT NULL DEFAULT 0,
              max_scope TEXT NOT NULL DEFAULT 'limited',
              review_delegate TEXT,
              allowed_files_json TEXT NOT NULL DEFAULT '[]',
              forbidden_files_json TEXT NOT NULL DEFAULT '[]',
              context_files_json TEXT NOT NULL DEFAULT '[]',
              acceptance_json TEXT NOT NULL DEFAULT '[]',
              claimed_by TEXT,
              claimed_at TEXT,
              lock_id TEXT,
              attempt INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 2,
              timeout_minutes INTEGER NOT NULL DEFAULT 30,
              max_budget_usd REAL,
              content_hash TEXT,
              frontmatter_hash TEXT,
              legacy_imported INTEGER NOT NULL DEFAULT 0,
              legacy_source_path TEXT,
              git_branch TEXT,
              worktree_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            );
            INSERT INTO tasks_old SELECT * FROM tasks;
            DROP TABLE events;
            CREATE TABLE events (
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
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(message_id) REFERENCES "tasks_old"(id)
            );
            INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
            VALUES ('B0-schema-hardening', 'partial-existing', 'Fake', 'task_done', 'done', 'x.md', '{}', '2026-06-04T00:00:00Z');
            """
        )

    init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        table_schemas = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        assert "tasks_old" not in {name for name, _sql in table_schemas}
        assert all("tasks_old" not in str(sql) for _name, sql in table_schemas)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT message_id FROM events").fetchall() == [("partial-existing",)]


def test_gitignore_excludes_runtime_state_and_python_cache_before_git_steward() -> None:
    gitignore_path = PROJECT_ROOT / ".gitignore"

    assert gitignore_path.exists(), "Git Steward 前必须先定义 .gitignore，避免运行状态误入版本流。"
    ignored_patterns = set(gitignore_path.read_text(encoding="utf-8").splitlines())

    assert "__pycache__/" in ignored_patterns
    assert "*.py[cod]" in ignored_patterns
    assert ".pytest_cache/" in ignored_patterns
    assert "docs/ai-workgroup/state/" in ignored_patterns
    assert "*.sqlite" in ignored_patterns
    assert "*.sqlite-wal" in ignored_patterns
    assert "*.sqlite-shm" in ignored_patterns

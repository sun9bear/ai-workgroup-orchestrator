from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from aiwg.protocol.frontmatter import parse_message_file
from aiwg.protocol.schema import validate_message_file

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "messages"
VALID_FIXTURE = FIXTURE_DIR / "valid-review-ready.md"
INVALID_ALLOWED_FILES_FIXTURE = FIXTURE_DIR / "invalid-can-write-allowed-files.md"


def write_message(tmp_path: Path, frontmatter: str, body: str = "# Test\n") -> Path:
    path = tmp_path / "message.md"
    normalized_frontmatter = dedent(frontmatter).strip()
    path.write_text(f"---\n{normalized_frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


def test_parse_message_file_returns_frontmatter_and_body() -> None:
    parsed = parse_message_file(VALID_FIXTURE)

    assert parsed.path == VALID_FIXTURE
    assert parsed.frontmatter["id"] == "T0-msg-001"
    assert parsed.frontmatter["from"] == "CodeX"
    assert parsed.frontmatter["to"] == "OpenCode"
    assert parsed.frontmatter["requires_human"] is False
    assert parsed.frontmatter["can_write"] is False
    assert parsed.frontmatter["created_at"] == "2026-05-27T11:45:00+08:00"
    assert parsed.frontmatter["context_files"] == [
        "docs/plans/2026-05-25-ai-agent-collaboration-orchestration-plan.md"
    ]
    assert parsed.frontmatter["allowed_files"] == []
    assert "# Review Fixture" in parsed.body
    assert "Validate this review-only message fixture" in parsed.body


def test_schema_accepts_existing_valid_fixture() -> None:
    result = validate_message_file(VALID_FIXTURE)

    assert result.valid is True
    assert result.errors == []


def test_schema_rejects_allowed_files_when_message_is_read_only() -> None:
    result = validate_message_file(INVALID_ALLOWED_FILES_FIXTURE)

    assert result.valid is False
    assert any(
        "allowed_files" in error and "can_write is false" in error
        for error in result.errors
    )


def test_schema_requires_allowed_files_for_write_messages(tmp_path: Path) -> None:
    message_path = write_message(
        tmp_path,
        """
        id: T0-msg-003
        task: T0
        from: CodeX
        to: Claude-Code
        type: instruction
        status: ready
        priority: medium
        requires_human: false
        created_at: 2026-05-27T11:45:00+08:00
        can_write: true
        allowed_files: []
        forbidden_files:
          - .env
        """,
    )

    result = validate_message_file(message_path)

    assert result.valid is False
    assert any(
        "allowed_files" in error and "can_write is true" in error
        for error in result.errors
    )


def test_schema_rejects_missing_required_fields_invalid_enums_and_bad_numbers(
    tmp_path: Path,
) -> None:
    message_path = write_message(
        tmp_path,
        """
        id: T0-msg-004
        task: T0
        from: Unknown-Agent
        to: OpenCode
        type: review
        status: not_a_status
        priority: urgent
        requires_human: maybe
        created_at: not-a-date
        can_write: false
        attempt: -1
        max_attempts: 0
        timeout_minutes: 0
        """,
    )

    result = validate_message_file(message_path)

    assert result.valid is False
    assert "Invalid from 'Unknown-Agent'." in result.errors
    assert "Invalid status 'not_a_status'." in result.errors
    assert "Invalid priority 'urgent'." in result.errors
    assert "Field 'requires_human' must be true or false." in result.errors
    assert any("created_at" in error and "ISO 8601" in error for error in result.errors)
    assert "Field 'attempt' must be a non-negative integer." in result.errors
    assert "Field 'max_attempts' must be a positive integer." in result.errors
    assert "Field 'timeout_minutes' must be a positive integer." in result.errors


def test_schema_reports_frontmatter_parse_errors_without_traceback(tmp_path: Path) -> None:
    message_path = tmp_path / "broken.md"
    message_path.write_text(
        "---\nid: T0-msg-005\n  task: bad-indent\n---\n\n# Broken\n",
        encoding="utf-8",
    )

    result = validate_message_file(message_path)

    assert result.valid is False
    assert any("Invalid YAML front matter" in error for error in result.errors)


def test_cli_validate_message_matches_legacy_ok_and_err_output() -> None:
    valid = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "validate-message",
            str(VALID_FIXTURE),
            "--config",
            "aiwg.yaml",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    invalid = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "validate-message",
            str(INVALID_ALLOWED_FILES_FIXTURE),
            "--config",
            "aiwg.yaml",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert valid.returncode == 0, valid.stderr
    assert f"OK  {VALID_FIXTURE}" in valid.stdout
    assert invalid.returncode == 1
    assert f"ERR {INVALID_ALLOWED_FILES_FIXTURE}" in invalid.stdout
    assert "Field 'allowed_files' must be empty when can_write is false" in invalid.stdout

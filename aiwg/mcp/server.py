from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from aiwg.mcp.tools import (
    READ_ONLY_TOOL_NAMES,
    get_task_tool,
    list_tasks_tool,
    recent_events_tool,
    status_tool,
)

MCP_SDK_ERROR = (
    "MCP SDK is not installed. Install optional dependency with `pip install mcp` "
    "or run a packaging extra once it is added. Phase C read-only tool listing "
    "still works with --list-tools."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aiwg.mcp.server",
        description="Phase C read-only MCP server for AI Workgroup Orchestrator v2.",
        epilog=(
            "Read-only tools exposed: "
            + ", ".join(READ_ONLY_TOOL_NAMES)
            + ". Only these read-only tools are registered in Phase C0/C1."
        ),
    )
    parser.add_argument(
        "--config",
        default="aiwg.yaml",
        help="Path to aiwg YAML config (default: aiwg.yaml).",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List Phase C read-only tool names and exit without requiring the MCP SDK.",
    )
    parser.add_argument(
        "--require-sdk-check-only",
        action="store_true",
        help="Check whether the optional MCP SDK is importable, then exit without starting a server.",
    )
    return parser


def _load_fastmcp() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via main return code
        raise RuntimeError(MCP_SDK_ERROR) from exc
    return FastMCP


def _print_tools() -> None:
    for name in READ_ONLY_TOOL_NAMES:
        print(name)


def _register_read_only_tools(app: Any, *, config_path: str) -> None:
    @app.tool(name="status")
    def status(
        recent_events: int = 10,
        task_limit: int = 50,
        status_filter: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Read AIWG SQLite status without mutating state."""

        return status_tool(
            config_path=config_path,
            recent_events=recent_events,
            task_limit=task_limit,
            status_filter=status_filter,
            agent=agent,
        )

    @app.tool(name="list_tasks")
    def list_tasks(
        status_filter: str | None = None,
        agent: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List AIWG tasks from SQLite without mutating state."""

        return list_tasks_tool(
            config_path=config_path,
            status_filter=status_filter,
            agent=agent,
            limit=limit,
        )

    @app.tool(name="get_task")
    def get_task(task_id: str) -> dict[str, Any]:
        """Get one AIWG task from SQLite without mutating state."""

        return get_task_tool(config_path=config_path, task_id=task_id)

    @app.tool(name="recent_events")
    def recent_events(limit: int = 10) -> dict[str, Any]:
        """List recent AIWG events from SQLite without mutating state."""

        return recent_events_tool(config_path=config_path, limit=limit)


def run_server(*, config_path: str) -> int:
    FastMCP = _load_fastmcp()
    app = FastMCP("ai-workgroup-orchestrator")
    _register_read_only_tools(app, config_path=config_path)
    app.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_tools:
        _print_tools()
        return 0

    if args.require_sdk_check_only:
        try:
            _load_fastmcp()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("MCP SDK available")
        return 0

    try:
        return run_server(config_path=str(args.config))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

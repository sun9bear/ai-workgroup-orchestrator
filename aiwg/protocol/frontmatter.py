from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


class FrontMatterError(ValueError):
    """Raised when a Markdown message does not contain valid YAML front matter."""


@dataclass(frozen=True)
class ParsedMarkdownMessage:
    path: Path | None
    frontmatter: dict[str, Any]
    body: str


def parse_message_file(path: Path | str) -> ParsedMarkdownMessage:
    message_path = Path(path)
    text = message_path.read_text(encoding="utf-8")
    return parse_message_text(text, path=message_path)


def parse_message_text(text: str, *, path: Path | str | None = None) -> ParsedMarkdownMessage:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise FrontMatterError("Missing opening front matter delimiter.")

    end_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        -1,
    )
    if end_index < 0:
        raise FrontMatterError("Missing closing front matter delimiter.")

    frontmatter_text = "\n".join(lines[1:end_index])
    try:
        loaded = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise FrontMatterError(f"Invalid YAML front matter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise FrontMatterError("Front matter must be a YAML mapping.")

    body = "\n".join(lines[end_index + 1 :])
    return ParsedMarkdownMessage(
        path=Path(path) if path is not None else None,
        frontmatter=_normalize_yaml_value(loaded),
        body=body,
    )


def _normalize_yaml_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_yaml_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_yaml_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value

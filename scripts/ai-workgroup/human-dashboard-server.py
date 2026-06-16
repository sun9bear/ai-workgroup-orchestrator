#!/usr/bin/env python3
"""
Local Human dashboard for the AI workgroup file protocol.

This intentionally uses only the Python standard library. It reads markdown
message files from docs/ai-workgroup and writes Human -> CodeX decision cards.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_PROJECT_ROOT = r"D:\example\protected-business-repo"
DEFAULT_WORKGROUP_RELATIVE = "docs/ai-workgroup"
DEFAULT_FORBIDDEN = [".env", "migrations/**", "docs/ai-workgroup/state/**"]
MESSAGE_DIRS = ("inbox", "working", "done")
ROLE_READY_STALE_SECONDS = 10 * 60
ROLE_AGENTS = {"CodeX", "Reviewer", "Git-Steward", "Claude-Code"}
ROLE_NUDGE_TARGETS = {
    "CodeX": "TechLead",
    "Reviewer": "Reviewer",
    "Git-Steward": "GitSteward",
}


def now_iso() -> str:
    stamp = dt.datetime.now().astimezone()
    return stamp.isoformat(timespec="seconds")


def file_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "task"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "[]":
        return []
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace(r"\"", '"')
    return value


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    front: dict[str, Any] = {}
    current_key = ""
    for raw in lines[1:end]:
        if raw.startswith("  - ") and current_key:
            front.setdefault(current_key, [])
            if not isinstance(front[current_key], list):
                front[current_key] = []
            front[current_key].append(parse_scalar(raw[4:]))
            continue

        if ":" not in raw:
            continue

        key, value = raw.split(":", 1)
        key = key.strip()
        if not key:
            continue
        current_key = key
        front[key] = parse_scalar(value)

    return front, "\n".join(lines[end + 1 :]).strip()


def format_list(key: str, values: list[str]) -> str:
    if not values:
        return f"{key}: []"
    lines = [f"{key}:"]
    for value in values:
        lines.append(f"  - {value}")
    return "\n".join(lines)


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def message_time(message: dict[str, Any]) -> dt.datetime:
    created_at = str(message.get("created_at") or "").strip()
    if created_at:
        try:
            return dt.datetime.fromisoformat(created_at)
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(float(message.get("mtime") or 0), tz=dt.datetime.now().astimezone().tzinfo)


def task_family(task: str) -> str:
    normalized = re.sub(r"-r\d+(?=-)", "", task or "")
    normalized = re.sub(r"-msg-\d{4}-\d{2}-\d{2}T.*$", "", normalized)
    return normalized


def status_rank(status: str) -> int:
    order = {
        "ready": 0,
        "claimed": 1,
        "reported": 2,
        "needs_human": 3,
        "needs_clarification": 4,
        "needs_review": 5,
        "failed": 6,
        "stale_claim": 7,
        "done": 8,
        "cancelled": 9,
    }
    return order.get(status, 50)


def is_informational_report(message: dict[str, Any]) -> bool:
    return (
        message.get("type") == "report"
        and message.get("to") == "Human"
        and not message.get("needs_human")
    )


@dataclass
class DashboardConfig:
    project_root: Path
    workgroup_root: Path
    orchestrator_root: Path
    host: str
    port: int


class WorkgroupStore:
    def __init__(self, config: DashboardConfig):
        self.config = config

    def rel_path(self, path: Path) -> str:
        return path.relative_to(self.config.project_root).as_posix()

    def assert_inside_workgroup(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.config.project_root / raw_path
        resolved = path.resolve()
        root = self.config.workgroup_root.resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError("Path is outside the configured workgroup root.")
        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        return resolved

    def scan_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for folder in MESSAGE_DIRS:
            base = self.config.workgroup_root / folder
            if not base.exists():
                continue
            for path in base.rglob("*.md"):
                try:
                    text = read_text(path)
                    front, body = parse_front_matter(text)
                    stat = path.stat()
                    rel = self.rel_path(path)
                    lane = path.relative_to(self.config.workgroup_root).parts
                    area = lane[0] if lane else ""
                    agent = lane[1] if len(lane) > 1 and area in ("inbox", "working") else str(front.get("to", ""))
                    status = str(front.get("status", "unknown"))
                    requires_human = bool_value(front.get("requires_human", False))
                    to_agent = str(front.get("to", ""))
                    messages.append(
                        {
                            "id": str(front.get("id", "")),
                            "task": str(front.get("task", "")),
                            "from": str(front.get("from", "")),
                            "to": to_agent,
                            "type": str(front.get("type", "")),
                            "status": status,
                            "priority": str(front.get("priority", "")),
                            "requires_human": requires_human,
                            "can_write": bool_value(front.get("can_write", False)),
                            "created_at": str(front.get("created_at", "")),
                            "reply_to": str(front.get("reply_to", "")),
                            "allowed_files": front.get("allowed_files", []),
                            "forbidden_files": front.get("forbidden_files", []),
                            "context_files": front.get("context_files", []),
                            "area": area,
                            "agent": agent,
                            "path": str(path),
                            "relative_path": rel,
                            "mtime": stat.st_mtime,
                            "mtime_iso": dt.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                            "body_excerpt": body[:420],
                            "needs_human": status == "ready" and requires_human and to_agent == "Human",
                        }
                    )
                except Exception as exc:  # keep the dashboard available despite bad files
                    messages.append(
                        {
                            "id": "",
                            "task": path.name,
                            "from": "",
                            "to": "",
                            "type": "parse_error",
                            "status": "parse_error",
                            "priority": "",
                            "requires_human": True,
                            "can_write": False,
                            "created_at": "",
                            "reply_to": "",
                            "allowed_files": [],
                            "forbidden_files": [],
                            "context_files": [],
                            "area": "error",
                            "agent": "",
                            "path": str(path),
                            "relative_path": self.rel_path(path),
                            "mtime": path.stat().st_mtime if path.exists() else 0,
                            "mtime_iso": "",
                            "body_excerpt": str(exc),
                            "needs_human": True,
                        }
                    )
        messages.sort(key=lambda item: (item["needs_human"], -status_rank(item["status"]), item["mtime"]), reverse=True)
        return messages

    def recent_events(self, limit: int = 80) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        state_dir = self.config.workgroup_root / "state"
        if not state_dir.exists():
            return events
        for path in state_dir.glob("events*.jsonl"):
            try:
                for line in read_text(path).splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"raw": line}
                    event["_source"] = self.rel_path(path)
                    events.append(event)
            except Exception:
                continue
        events.sort(key=lambda item: str(item.get("ts") or item.get("timestamp") or ""), reverse=True)
        return events[:limit]

    def covered_failure(self, failed_message: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        family = task_family(str(failed_message.get("task") or ""))
        if not family:
            return None
        failed_time = message_time(failed_message)
        candidates: list[dict[str, Any]] = []
        for message in messages:
            if message is failed_message:
                continue
            if task_family(str(message.get("task") or "")) != family:
                continue
            if message_time(message) <= failed_time:
                continue
            if str(message.get("status") or "") in ("done", "reported"):
                candidates.append(message)
            elif message.get("type") == "report" and str(message.get("to") or "") == "CodeX":
                candidates.append(message)
        if not candidates:
            return None
        candidates.sort(key=lambda item: message_time(item), reverse=True)
        winner = candidates[0]
        return {
            "task": winner.get("task", ""),
            "status": winner.get("status", ""),
            "relative_path": winner.get("relative_path", ""),
            "mtime_iso": winner.get("mtime_iso", ""),
        }

    def mechanism_alerts(self, workflow_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        now = dt.datetime.now().astimezone()
        for message in workflow_messages:
            if message.get("covered_failure"):
                alerts.append(
                    {
                        "severity": "info",
                        "kind": "covered_failed",
                        "task": message.get("task", ""),
                        "message": (
                            f"历史失败任务 {message.get('task', '')} 已被后续任务覆盖，"
                            "已从活跃任务中隐藏；可由 Tech Lead 后续归档或标记 superseded。"
                        ),
                    }
                )
                continue

            status = str(message.get("status") or "")
            area = str(message.get("area") or "")
            to_agent = str(message.get("to") or "")
            if status == "failed":
                alerts.append(
                    {
                        "severity": "danger",
                        "kind": "uncovered_failed",
                        "task": message.get("task", ""),
                        "source_path": message.get("relative_path", ""),
                        "nudge_role": "TechLead",
                        "message": f"发现未覆盖的失败任务 {message.get('task', '')}，需要对应角色或 Tech Lead 处理。",
                    }
                )

            if (
                status == "ready"
                and area == "inbox"
                and to_agent in ROLE_AGENTS
                and not is_informational_report(message)
            ):
                age_seconds = max(0, int((now - message_time(message)).total_seconds()))
                if age_seconds >= ROLE_READY_STALE_SECONDS:
                    minutes = age_seconds // 60
                    if to_agent == "Claude-Code":
                        detail = "Claude-Code 计划任务可能未消费；可以检查/启动 Implementer 或 Advisor runner。"
                    else:
                        detail = f"{to_agent} 桌面角色 heartbeat 可能未实际唤醒；不要代替该角色执行，只需唤醒或告警。"
                    alerts.append(
                        {
                            "severity": "warning",
                            "kind": "stale_ready",
                            "agent": to_agent,
                            "task": message.get("task", ""),
                            "source_path": message.get("relative_path", ""),
                            "nudge_role": ROLE_NUDGE_TARGETS.get(to_agent, ""),
                            "age_minutes": minutes,
                            "message": f"{to_agent} ready 任务 {message.get('task', '')} 已等待约 {minutes} 分钟。{detail}",
                        }
                    )
        return alerts

    def summary(self) -> dict[str, Any]:
        messages = self.scan_messages()
        workflow_messages = [m for m in messages if m["area"] in ("inbox", "working")]
        for message in workflow_messages:
            if message["status"] == "failed":
                covered_by = self.covered_failure(message, messages)
                if covered_by:
                    message["covered_failure"] = True
                    message["covered_by"] = covered_by

        counts: dict[str, int] = {}
        by_agent: dict[str, int] = {}
        for message in workflow_messages:
            if message.get("covered_failure"):
                status_for_stats = "covered_failed"
            else:
                status_for_stats = "reported" if is_informational_report(message) else message["status"]
            counts[status_for_stats] = counts.get(status_for_stats, 0) + 1
            if message["area"] in ("inbox", "working"):
                key = f"{message['area']}/{message['agent']}"
                by_agent[key] = by_agent.get(key, 0) + 1

        human_pending = [m for m in workflow_messages if m["needs_human"]]
        ready = [
            m
            for m in workflow_messages
            if m["status"] == "ready" and m["area"] == "inbox" and not is_informational_report(m)
        ]
        # `reported` is an audit/review artifact, not work that is still active.
        # Keep it in recent, but do not make the Human dashboard look stuck.
        active_statuses = ("ready", "claimed", "working", "needs_clarification", "needs_review", "failed", "stale_claim")
        active = [
            m
            for m in workflow_messages
            if m["status"] in active_statuses and not is_informational_report(m) and not m.get("covered_failure")
        ]
        alerts = self.mechanism_alerts(workflow_messages)
        return {
            "generated_at": now_iso(),
            "project_root": str(self.config.project_root),
            "workgroup_root": str(self.config.workgroup_root),
            "counts": counts,
            "by_agent": by_agent,
            "mechanism_alerts": alerts,
            "human_pending": human_pending[:20],
            "ready": ready[:40],
            "active": active[:80],
            "recent": sorted(messages, key=lambda item: item["mtime"], reverse=True)[:80],
            "events": self.recent_events(80),
        }

    def read_message(self, raw_path: str) -> dict[str, Any]:
        path = self.assert_inside_workgroup(raw_path)
        text = read_text(path)
        front, body = parse_front_matter(text)
        return {
            "frontmatter": front,
            "body": body,
            "path": str(path),
            "relative_path": self.rel_path(path),
        }

    def update_status(self, path: Path, status: str) -> None:
        text = read_text(path)
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("Message has no front matter.")
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is None:
            raise ValueError("Message front matter is not closed.")
        changed = False
        for i in range(1, end):
            if lines[i].startswith("status:"):
                lines[i] = f"status: {status}"
                changed = True
                break
        if not changed:
            lines.insert(end, f"status: {status}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def create_human_decision(self, message_path: str, action: str, note: str) -> dict[str, Any]:
        source = self.assert_inside_workgroup(message_path)
        source_doc = self.read_message(str(source))
        front = source_doc["frontmatter"]
        original_task = str(front.get("task") or source.stem)
        original_id = str(front.get("id") or "")

        action_titles = {
            "approve_recommended": "批准推荐方案",
            "request_clarification": "要求澄清",
            "pause_task": "暂停任务",
        }
        if action not in action_titles:
            raise ValueError(f"Unsupported Human action: {action}")

        task = f"{original_task}-human-decision"
        message_id = f"{safe_name(task)}-msg-{file_stamp()}"
        timestamp = file_stamp()
        file_name = f"{timestamp}_from-Human_to-CodeX_type-instruction_task-{safe_name(task)}.md"
        inbox = self.config.workgroup_root / "inbox" / "CodeX"
        inbox.mkdir(parents=True, exist_ok=True)
        out_path = inbox / file_name

        rel_source = self.rel_path(source)
        decision = action_titles[action]
        if action == "approve_recommended":
            instruction = (
                "Human 批准该报告的推荐方案。CodeX 应优先读取当前 phase-envelope.current.json，"
                "若后续任务处于已批准阶段授权包的 approved_scope、allowed_files 和 forbidden_files 约束内，"
                "可以继续创建窄范围 Claude-Code 实现任务，不必再次请求 Human。"
                "只有触发 human_required_if、越界、方案歧义或真实生产操作时，才回到 Human Gate。"
                "后续报告和设计文档默认使用简体中文；代码符号、路径、命令和状态值保持英文。"
            )
        elif action == "request_clarification":
            instruction = (
                "Human 要求先澄清问题；在澄清前不要创建后续实现任务。"
                "后续说明默认使用简体中文；代码符号、路径、命令和状态值保持英文。"
            )
        else:
            instruction = "Human 暂停此任务；除非 Human 发送新的决策任务，否则不要创建后续实现任务。"

        note = note.strip() or "（无额外批注）"
        body = f"""# Human 决策：{original_task}

决策：`{action}`（{decision}）

原始报告：

- `{rel_source}`
- reply_to: `{original_id}`

给 CodeX 的指令：

{instruction}

Human 批注：

{note}
"""

        content = f"""---
id: {message_id}
task: {task}
from: Human
to: CodeX
type: instruction
status: ready
priority: high
reply_to: "{original_id}"
requires_human: false
created_at: {now_iso()}
can_write: false
{format_list("context_files", [rel_source])}
allowed_files: []
{format_list("forbidden_files", DEFAULT_FORBIDDEN)}
attempt: 0
max_attempts: 1
timeout_minutes: 30
review_delegate: CodeX
---

{body}
"""
        out_path.write_text(content, encoding="utf-8")
        self.update_status(source, "done")
        return {
            "ok": True,
            "path": str(out_path),
            "relative_path": self.rel_path(out_path),
            "message_id": message_id,
            "source_status": "done",
        }

    def run_next_claude_task(self) -> dict[str, Any]:
        script = self.config.orchestrator_root / "scripts" / "ai-workgroup" / "Invoke-ProjectCoordinatorOnce.ps1"
        if not script.exists():
            raise FileNotFoundError(str(script))

        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ProjectRoot",
            str(self.config.project_root),
            "-AllowWrite",
            "-Json",
        ]
        started = now_iso()
        completed = subprocess.run(
            command,
            cwd=str(self.config.orchestrator_root),
            text=True,
            capture_output=True,
            timeout=1500,
        )
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        parsed: Any = None
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None
        return {
            "ok": completed.returncode == 0,
            "started_at": started,
            "finished_at": now_iso(),
            "exit_code": completed.returncode,
            "stdout": stdout[-6000:],
            "stderr": stderr[-3000:],
            "result": parsed,
        }

    def nudge_role(self, role: str, reason: str, task: str, source_path: str) -> dict[str, Any]:
        allowed_roles = {"TechLead", "Reviewer", "GitSteward"}
        if role not in allowed_roles:
            raise ValueError(f"Unsupported nudge role: {role}")
        return {
            "ok": False,
            "action": "disabled",
            "role": role,
            "task": task,
            "source_path": source_path,
            "message": "Role nudge is disabled by policy; dashboard will not modify Codex automation tasks.",
        }


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI 协作看板</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #667085;
      --line: #d8dee8;
      --accent: #1d6f8f;
      --danger: #b42318;
      --danger-bg: #fff0ef;
      --warn: #9a5b00;
      --warn-bg: #fff7e6;
      --ok: #237a4b;
      --dark: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", Arial, sans-serif;
    }
    header {
      background: var(--dark);
      color: #fff;
      padding: 14px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { font-size: 18px; margin: 0; font-weight: 650; }
    h2 { font-size: 15px; margin: 0 0 10px; }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      padding: 7px 10px;
      cursor: pointer;
      font: inherit;
    }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    button.danger { background: var(--danger); color: #fff; border-color: var(--danger); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .meta { color: #c7d1df; font-size: 12px; }
    .wrap { max-width: 1440px; margin: 0 auto; padding: 16px; }
    .banner {
      display: none;
      background: var(--danger-bg);
      border: 1px solid #ffb4ad;
      color: var(--danger);
      padding: 12px 14px;
      border-radius: 8px;
      margin-bottom: 14px;
      font-weight: 650;
    }
    .mechanism-alerts {
      display: none;
      margin-bottom: 14px;
    }
    .mechanism-alert {
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 8px;
      font-weight: 600;
    }
    .mechanism-alert-line {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .nudge-btn {
      white-space: nowrap;
      background: #fff;
      font-weight: 500;
    }
    .mechanism-alert.warning {
      background: var(--warn-bg);
      border-color: #ffd28a;
      color: var(--warn);
    }
    .mechanism-alert.danger {
      background: var(--danger-bg);
      border-color: #ffb4ad;
      color: var(--danger);
    }
    .mechanism-alert.info {
      background: #eef8ff;
      border-color: #b7dff5;
      color: var(--accent);
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 14px;
    }
    .stat {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
    }
    .stat strong { display: block; font-size: 22px; }
    .stat span { color: var(--muted); font-size: 12px; }
    .list { display: grid; gap: 8px; }
    .row {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      cursor: pointer;
    }
    .row:hover { border-color: #9bb8cf; }
    .row.human { border-color: #ffb4ad; background: var(--danger-bg); }
    .row-title { font-weight: 650; margin-bottom: 4px; overflow-wrap: anywhere; }
    .row-meta { color: var(--muted); font-size: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 7px;
      background: #fff;
      color: var(--muted);
    }
    .pill.ready { color: var(--accent); border-color: #a9c9d6; }
    .pill.human { color: var(--danger); border-color: #ffb4ad; }
    .detail-body {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      border: 1px solid var(--line);
      background: #fbfcfd;
      border-radius: 8px;
      padding: 12px;
      min-height: 220px;
      max-height: 520px;
      overflow: auto;
    }
    .detail-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
      margin-bottom: 10px;
    }
    .path {
      color: var(--muted);
      font-family: Consolas, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    textarea {
      width: 100%;
      min-height: 78px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      font: inherit;
      resize: vertical;
      margin: 8px 0;
    }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
    .events {
      display: grid;
      gap: 6px;
      max-height: 360px;
      overflow: auto;
    }
    .event { font-size: 12px; color: var(--muted); border-bottom: 1px solid #eef1f5; padding-bottom: 6px; }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>AI 协作看板</h1>
      <div class="meta" id="rootMeta">加载中...</div>
    </div>
    <div class="actions">
      <button id="runNextBtn" class="primary">立即执行下一步</button>
      <button id="refreshBtn">刷新</button>
    </div>
  </header>
  <main class="wrap">
    <div id="humanBanner" class="banner"></div>
    <div id="mechanismAlerts" class="mechanism-alerts"></div>
    <section class="stats" id="stats"></section>
    <section class="grid">
      <div class="panel">
        <h2>待人工决策</h2>
        <div id="humanList" class="list"></div>
        <h2 style="margin-top:18px">活跃任务</h2>
        <div id="activeList" class="list"></div>
      </div>
      <div class="panel">
        <div class="detail-head">
          <div>
            <h2 id="detailTitle">选择一条消息</h2>
            <div class="path" id="detailPath"></div>
          </div>
          <div class="row-meta" id="detailMeta"></div>
        </div>
        <div id="humanActions" style="display:none">
          <textarea id="decisionNote" placeholder="人工批注意见，可选。例如：批准推荐方案 1，只做 design-only boundary/spec，不写业务代码。"></textarea>
          <div class="actions">
            <button class="primary" data-action="approve_recommended">批准推荐方案</button>
            <button data-action="request_clarification">要求澄清</button>
            <button class="danger" data-action="pause_task">暂停任务</button>
          </div>
        </div>
        <div class="detail-body" id="detailBody">待处理消息会显示在这里。</div>
      </div>
    </section>
    <section class="panel" style="margin-top:14px">
      <h2>立即执行</h2>
      <div class="detail-body" id="runOutput" style="min-height:80px; max-height:240px;">点击“立即执行下一步”会先扫描 Claude-Code 可执行任务；如果队列已清空，会自动创建 CodeX 下一阶段拆解/派发任务。遇到 Human Gate 会停下来等待人工批复。</div>
    </section>
    <section class="panel" style="margin-top:14px">
      <h2>最近事件</h2>
      <div id="events" class="events"></div>
    </section>
  </main>
  <script>
    let selectedPath = "";
    let selectedNeedsHuman = false;

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    async function api(path, options) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }

    function renderStats(summary) {
      const counts = summary.counts || {};
      const items = [
        ["待人工", summary.human_pending.length],
        ["待处理", counts.ready || 0],
        ["执行中", (counts.claimed || 0) + (counts.working || 0)],
        ["已报告", counts.reported || 0],
      ];
      document.getElementById("stats").innerHTML = items.map(([label, value]) =>
        `<div class="stat"><strong>${value}</strong><span>${label}</span></div>`
      ).join("");
    }

    function renderMechanismAlerts(alerts) {
      const el = document.getElementById("mechanismAlerts");
      if (!alerts || !alerts.length) {
        el.style.display = "none";
        el.innerHTML = "";
        return;
      }
      el.style.display = "block";
      el.innerHTML = alerts.map(a => {
        const button = a.nudge_role
          ? `<button class="nudge-btn" disabled title="已按策略禁用：看板不再修改 Codex 自动化任务">唤醒已禁用</button>`
          : "";
        return `<div class="mechanism-alert ${escapeHtml(a.severity || "info")}">
          <div class="mechanism-alert-line">
            <span>${escapeHtml(a.message || "")}</span>
            ${button}
          </div>
        </div>`;
      }).join("");
    }

    async function nudgeRole(role, task, sourcePath, reason) {
      const out = document.getElementById("runOutput");
      out.textContent = `正在唤醒 ${role}...`;
      try {
        const result = await api("/api/nudge-role", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({role, task, source_path: sourcePath, reason})
        });
        const parsed = result.result || {};
        out.textContent = [
          `已请求唤醒：${role}`,
          parsed.action ? `动作：${parsed.action}` : "",
          parsed.path ? `automation：${parsed.path}` : "",
          result.stderr ? `错误输出：\n${result.stderr}` : "",
        ].filter(Boolean).join("\n\n");
      } catch (err) {
        out.textContent = `唤醒失败：${err.message}`;
      } finally {
        await refresh();
      }
    }

    function renderList(id, messages, emptyText) {
      const el = document.getElementById(id);
      if (!messages.length) {
        el.innerHTML = `<div class="row"><div class="row-meta">${escapeHtml(emptyText)}</div></div>`;
        return;
      }
      el.innerHTML = messages.map(m => {
        const classes = ["row", m.needs_human ? "human" : ""].join(" ");
        return `<div class="${classes}" data-path="${escapeHtml(m.path)}">
          <div class="row-title">${escapeHtml(m.task || m.id || m.relative_path)}</div>
          <div class="row-meta">
            <span class="pill ${escapeHtml(m.status)}">${escapeHtml(m.status)}</span>
            ${m.needs_human ? '<span class="pill human">Human Gate</span>' : ''}
            <span>${escapeHtml(m.from)} -> ${escapeHtml(m.to)}</span>
            <span>${escapeHtml(m.mtime_iso)}</span>
          </div>
        </div>`;
      }).join("");
      for (const row of el.querySelectorAll(".row[data-path]")) {
        row.addEventListener("click", () => loadMessage(row.dataset.path));
      }
    }

    function renderEvents(events) {
      const el = document.getElementById("events");
      if (!events.length) {
        el.innerHTML = '<div class="event">No events.</div>';
        return;
      }
      el.innerHTML = events.slice(0, 60).map(e => {
        const label = e.type || e.event || e.raw || "event";
        const agent = e.agent ? ` · ${e.agent}` : "";
        const status = e.status ? ` · ${e.status}` : "";
        const ts = e.ts || e.timestamp || "";
        return `<div class="event">${escapeHtml(ts)} ${escapeHtml(label)}${escapeHtml(agent)}${escapeHtml(status)}</div>`;
      }).join("");
    }

    async function refresh() {
      const summary = await api("/api/summary");
      document.getElementById("rootMeta").textContent = `${summary.project_root} · ${summary.generated_at}`;
      const banner = document.getElementById("humanBanner");
      if (summary.human_pending.length) {
        banner.style.display = "block";
        banner.textContent = `${summary.human_pending.length} 个任务需要人工决策。`;
      } else {
        banner.style.display = "none";
      }
      renderMechanismAlerts(summary.mechanism_alerts || []);
      renderStats(summary);
      renderList("humanList", summary.human_pending, "当前没有等待人工决策的任务。");
      renderList("activeList", summary.active, "当前没有活跃任务。");
      renderEvents(summary.events || []);
    }

    async function loadMessage(path) {
      selectedPath = path;
      const data = await api(`/api/message?path=${encodeURIComponent(path)}`);
      const fm = data.frontmatter || {};
      selectedNeedsHuman = fm.status === "ready" && fm.to === "Human" && fm.requires_human === true;
      document.getElementById("detailTitle").textContent = fm.task || fm.id || "Message";
      document.getElementById("detailPath").textContent = data.relative_path;
      document.getElementById("detailMeta").innerHTML = `
        <span class="pill ${escapeHtml(fm.status)}">${escapeHtml(fm.status)}</span>
        <span class="pill">${escapeHtml(fm.from)} -> ${escapeHtml(fm.to)}</span>
      `;
      document.getElementById("humanActions").style.display = selectedNeedsHuman ? "block" : "none";
      document.getElementById("detailBody").textContent = data.body || "(empty body)";
    }

    async function sendDecision(action) {
      if (!selectedPath || !selectedNeedsHuman) return;
      const note = document.getElementById("decisionNote").value;
      const buttons = document.querySelectorAll("#humanActions button");
      buttons.forEach(b => b.disabled = true);
      try {
        const result = await api("/api/human/respond", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({message_path: selectedPath, action, note})
        });
        document.getElementById("detailBody").textContent =
          `人工批复已写入：\n${result.relative_path}\n\n原 Human 报告已标记为 done。CodeX heartbeat 会继续处理新的决策任务。`;
        document.getElementById("humanActions").style.display = "none";
        selectedNeedsHuman = false;
        await refresh();
      } catch (err) {
        alert(err.message);
      } finally {
        buttons.forEach(b => b.disabled = false);
      }
    }

    async function runNext() {
      const btn = document.getElementById("runNextBtn");
      const out = document.getElementById("runOutput");
      btn.disabled = true;
      out.textContent = "正在检查队列；有合规 Claude-Code 任务就执行，队列空闲则派发 CodeX 下一阶段拆解任务...";
      try {
        const result = await api("/api/run-next", { method: "POST" });
        const parsed = result.result || {};
        const summary = [
          `完成时间：${result.finished_at}`,
          `退出码：${result.exit_code}`,
          parsed.action ? `动作：${parsed.action}` : "",
          parsed.reason ? `原因：${parsed.reason}` : "",
          parsed.message_id ? `消息：${parsed.message_id}` : "",
          result.stderr ? `错误输出：\n${result.stderr}` : "",
          result.stdout ? `原始输出：\n${result.stdout}` : "",
        ].filter(Boolean).join("\n\n");
        out.textContent = summary || "执行完成，但没有输出。";
        await refresh();
      } catch (err) {
        out.textContent = `执行失败：${err.message}`;
      } finally {
        btn.disabled = false;
      }
    }

    document.getElementById("refreshBtn").addEventListener("click", refresh);
    document.getElementById("runNextBtn").addEventListener("click", runNext);
    document.querySelectorAll("#humanActions button").forEach(btn => {
      btn.addEventListener("click", () => sendDecision(btn.dataset.action));
    });
    refresh();
    setInterval(refresh, 15000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    store: WorkgroupStore

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (dt.datetime.now().isoformat(timespec="seconds"), fmt % args))

    def send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_html(HTML)
                return
            if parsed.path == "/api/summary":
                self.send_json(self.store.summary())
                return
            if parsed.path == "/api/message":
                params = parse_qs(parsed.query)
                raw_path = params.get("path", [""])[0]
                if not raw_path:
                    self.send_json({"error": "path is required"}, 400)
                    return
                self.send_json(self.store.read_message(raw_path))
                return
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(body)
            if parsed.path == "/api/human/respond":
                result = self.store.create_human_decision(
                    str(payload.get("message_path", "")),
                    str(payload.get("action", "")),
                    str(payload.get("note", "")),
                )
                self.send_json(result)
                return
            if parsed.path == "/api/run-next":
                self.send_json(self.store.run_next_claude_task())
                return
            if parsed.path == "/api/nudge-role":
                self.send_json(
                    self.store.nudge_role(
                        str(payload.get("role", "")),
                        str(payload.get("reason", "")),
                        str(payload.get("task", "")),
                        str(payload.get("source_path", "")),
                    )
                )
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)


def build_config(args: argparse.Namespace) -> DashboardConfig:
    project_root = Path(args.project_root).resolve()
    workgroup_root = (project_root / args.workgroup_relative_path).resolve()
    if not project_root.exists():
        raise SystemExit(f"Project root does not exist: {project_root}")
    if not workgroup_root.exists():
        raise SystemExit(f"Workgroup root does not exist: {workgroup_root}")
    return DashboardConfig(
        project_root=project_root,
        workgroup_root=workgroup_root,
        orchestrator_root=Path(__file__).resolve().parents[2],
        host=args.host,
        port=args.port,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local AI workgroup Human dashboard.")
    parser.add_argument("--project-root", default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--workgroup-relative-path", default=DEFAULT_WORKGROUP_RELATIVE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--once", action="store_true", help="Print summary JSON and exit.")
    args = parser.parse_args()

    config = build_config(args)
    store = WorkgroupStore(config)
    if args.once:
        print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
        return 0

    DashboardHandler.store = store
    server = ThreadingHTTPServer((config.host, config.port), DashboardHandler)
    print(f"AI 协作看板: http://{config.host}:{config.port}")
    print(f"项目目录: {config.project_root}")
    print(f"协作目录: {config.workgroup_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

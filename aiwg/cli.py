from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import load_config, write_default_config
from aiwg.d5_preflight import evaluate_d5_preflight, render_d5_preflight_text
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.doctor import DoctorResult, run_doctor
from aiwg.external_review_gate import get_external_review_gate_snapshot, render_external_review_gate_text
from aiwg.git_steward import get_pr_gate_status, plan_git_dry_run
from aiwg.operator_approval import approve_preflight, approve_real_start, resume_preflight, revoke_real_start
from aiwg.protocol.schema import validate_message_file
from aiwg.role_health import get_role_health_snapshot, render_role_health_text
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database, resolve_project_root
from aiwg.state.importer import import_inbox, legacy_audit, list_tasks
from aiwg.write_gate import evaluate_write_gate_dry_run
from aiwg.workflow_contract import get_workflow_contract_snapshot, render_workflow_contract_text
from aiwg.workflow_preflight import get_workflow_status, plan_workflow_dry_run


DEFAULT_CONFIG = "aiwg.yaml"


def _add_common_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to aiwg YAML config (default: {DEFAULT_CONFIG})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiwg",
        description="AI Workgroup Orchestrator v2 control-plane CLI",
    )
    subcommands = parser.add_subparsers(dest="command")

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="Run Phase A0 safety and environment preflight checks.",
    )
    _add_common_config_arg(doctor_parser)
    doctor_parser.add_argument(
        "--project-root",
        default=".",
        help="Project root to inspect without mutation (default: current directory).",
    )
    doctor_parser.set_defaults(func=_doctor_command)

    init_parser = subcommands.add_parser(
        "init-config",
        help="Write a conservative default aiwg.yaml file.",
    )
    _add_common_config_arg(init_parser)
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file.",
    )
    init_parser.set_defaults(func=_init_config_command)

    validate_parser = subcommands.add_parser(
        "validate-message",
        help="Validate Markdown message front matter against the AIWG protocol schema.",
    )
    validate_parser.add_argument(
        "paths",
        nargs="+",
        help="One or more Markdown message files to validate.",
    )
    _add_common_config_arg(validate_parser)
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON validation results instead of legacy OK/ERR lines.",
    )
    validate_parser.set_defaults(func=_validate_message_command)

    init_db_parser = subcommands.add_parser(
        "init-db",
        help="Initialize or migrate the SQLite control-plane database.",
    )
    _add_common_config_arg(init_db_parser)
    init_db_parser.set_defaults(func=_init_db_command)

    import_parser = subcommands.add_parser(
        "import-inbox",
        help="Import Markdown inbox messages into SQLite tasks.",
    )
    _add_common_config_arg(import_parser)
    import_parser.add_argument("--agent", help="Only scan docs/ai-workgroup/inbox/<agent>.")
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report candidate messages without writing SQLite rows.",
    )
    import_parser.add_argument(
        "--manifest",
        help="Import only selected_candidates from a reviewed JSON manifest.",
    )
    import_parser.add_argument(
        "--evidence-only",
        action="store_true",
        help="Normalize imported manifest rows as non-dispatchable evidence (status=done, can_write=false, requires_human=true).",
    )
    import_parser.add_argument(
        "--approval-artifact",
        help="Machine-verifiable JSON approval artifact required for non-dry-run manifest imports.",
    )
    import_parser.set_defaults(func=_import_inbox_command)

    list_parser = subcommands.add_parser(
        "list-tasks",
        help="List SQLite tasks without mutating state.",
    )
    _add_common_config_arg(list_parser)
    list_parser.add_argument("--status", help="Filter by task status.")
    list_parser.add_argument("--agent", help="Filter by target agent.")
    list_parser.set_defaults(func=_list_tasks_command)

    status_parser = subcommands.add_parser(
        "status",
        help="Show a Phase A4 read-only task/event/artifact status snapshot.",
    )
    _add_common_config_arg(status_parser)
    status_parser.add_argument("--status", help="Filter task rows by status in the snapshot.")
    status_parser.add_argument("--agent", help="Filter task rows by target agent in the snapshot.")
    status_parser.add_argument(
        "--recent-events",
        type=int,
        default=10,
        help="Number of recent events to include (default: 10).",
    )
    status_parser.add_argument(
        "--task-limit",
        type=int,
        default=50,
        help="Maximum number of task rows and agent runs to include (default: 50).",
    )
    status_parser.add_argument("--json", action="store_true", help="Emit the read-only snapshot as JSON.")
    status_parser.set_defaults(func=_status_command)

    role_health_parser = subcommands.add_parser(
        "role-health",
        help="Show a D4.2 read-only role health contract snapshot.",
    )
    _add_common_config_arg(role_health_parser)
    role_health_parser.add_argument("--json", action="store_true", help="Emit the role health snapshot as JSON.")
    role_health_parser.set_defaults(func=_role_health_command)

    role_health_snapshot_parser = subcommands.add_parser(
        "role-health-snapshot",
        help="Emit the D4.2 read-only role health dashboard snapshot.",
    )
    _add_common_config_arg(role_health_snapshot_parser)
    role_health_snapshot_parser.add_argument("--json", action="store_true", help="Emit the snapshot as JSON.")
    role_health_snapshot_parser.set_defaults(func=_role_health_snapshot_command)

    external_review_gate_parser = subcommands.add_parser(
        "external-review-gate",
        help="Show a D4.3 read-only external review gate snapshot without PR mutation.",
    )
    _add_common_config_arg(external_review_gate_parser)
    external_review_gate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the external review gate snapshot as JSON.",
    )
    external_review_gate_parser.set_defaults(func=_external_review_gate_command)

    workflow_contract_parser = subcommands.add_parser(
        "workflow-contract",
        help="Show a D4.4 read-only versioned topology/workflow contract snapshot.",
    )
    _add_common_config_arg(workflow_contract_parser)
    workflow_contract_parser.add_argument("--topology", help="Override topology contract YAML path.")
    workflow_contract_parser.add_argument("--workflow", help="Override workflow contract YAML path.")
    workflow_contract_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the workflow contract snapshot as JSON.",
    )
    workflow_contract_parser.set_defaults(func=_workflow_contract_command)

    d5_preflight_parser = subcommands.add_parser(
        "d5-preflight",
        help="Run fake/dry-run D5 preflight evidence; no real agents or target writes.",
    )
    _add_common_config_arg(d5_preflight_parser)
    d5_preflight_parser.add_argument("--workflow-id", required=True, help="Workflow id to evaluate.")
    d5_preflight_parser.add_argument(
        "--target-root",
        required=True,
        help="Target/business repository root retained as read-only context only.",
    )
    d5_preflight_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Required: D5 only records fake/preflight evidence.",
    )
    d5_preflight_parser.add_argument(
        "--include-d5-1",
        action="store_true",
        help="Opt in to D5.1 budget/lease/external-review-fixture preflight controls.",
    )
    d5_preflight_parser.add_argument(
        "--external-review-fixture",
        help="Optional local read-only external review fixture JSON for D5.1 ingest preflight.",
    )
    d5_preflight_parser.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="Return exit code 3 when the dry-run preflight status is blocked.",
    )
    d5_preflight_parser.add_argument("--json", action="store_true", help="Emit JSON result.")
    d5_preflight_parser.set_defaults(func=_d5_preflight_command)

    readiness_parser = subcommands.add_parser(
        "adapter-readiness",
        help="Resolve real-adapter CLI binary availability and write a read-only readiness report.",
    )
    _add_common_config_arg(readiness_parser)
    readiness_parser.add_argument("--json", action="store_true", help="Emit the readiness report as JSON.")
    readiness_parser.add_argument(
        "--no-version-probe",
        action="store_true",
        help="Resolve binary paths only; do not run bounded --version probes.",
    )
    readiness_parser.set_defaults(func=_adapter_readiness_command)

    audit_parser = subcommands.add_parser(
        "legacy-audit",
        help="Write an audit-only report for legacy Markdown messages.",
    )
    _add_common_config_arg(audit_parser)
    audit_parser.set_defaults(func=_legacy_audit_command)

    run_once_parser = subcommands.add_parser(
        "run-once",
        help="Run one Phase A3 orchestrator tick: import, claim, dispatch Fake adapter.",
    )
    _add_common_config_arg(run_once_parser)
    run_once_parser.add_argument("--agent", default="Fake", help="Agent to run once (default: Fake).")
    run_once_parser.set_defaults(func=_run_once_command)

    approve_parser = subcommands.add_parser(
        "approve-preflight",
        help="Approve a restricted real-adapter preflight manifest without dispatching it.",
    )
    _add_common_config_arg(approve_parser)
    approve_parser.add_argument("--agent", required=True, help="Agent whose preflight manifest is being approved.")
    approve_parser.add_argument("--message-id", required=True, help="Message/task id to approve.")
    approve_parser.add_argument("--operator", required=True, help="Human operator identifier recorded in the audit log.")
    approve_parser.add_argument("--manifest", help="Optional explicit adapter-preflight.json path.")
    approve_parser.add_argument("--ttl-minutes", type=int, default=None, help="Approval time-to-live in minutes.")
    approve_parser.add_argument("--reason", default="", help="Optional human approval rationale.")
    approve_parser.set_defaults(func=_approve_preflight_command)

    approve_real_start_parser = subcommands.add_parser(
        "approve-real-start",
        help="Generate an explicit real-start authorization artifact after sandbox plan/probe validation.",
    )
    _add_common_config_arg(approve_real_start_parser)
    approve_real_start_parser.add_argument("--agent", required=True, help="Agent whose real-start gate is being authorized.")
    approve_real_start_parser.add_argument("--message-id", required=True, help="Message/task id to authorize.")
    approve_real_start_parser.add_argument("--operator", required=True, help="Human operator identifier recorded in the audit log.")
    approve_real_start_parser.add_argument("--sandbox-plan", required=True, help="Readiness-bound sandbox invocation plan path.")
    approve_real_start_parser.add_argument("--sandbox-report", required=True, help="Successful sandbox process report path.")
    approve_real_start_parser.add_argument("--ttl-minutes", type=int, default=None, help="Real-start authorization time-to-live in minutes.")
    approve_real_start_parser.add_argument("--reason", default="", help="Optional human authorization rationale.")
    approve_real_start_parser.set_defaults(func=_approve_real_start_command)

    revoke_real_start_parser = subcommands.add_parser(
        "revoke-real-start",
        help="Revoke an explicit real-start authorization artifact without starting any real agent process.",
    )
    _add_common_config_arg(revoke_real_start_parser)
    revoke_real_start_parser.add_argument("--agent", required=True, help="Agent whose real-start authorization is being revoked.")
    revoke_real_start_parser.add_argument("--message-id", required=True, help="Message/task id to revoke.")
    revoke_real_start_parser.add_argument("--operator", required=True, help="Human operator identifier recorded in the audit log.")
    revoke_real_start_parser.add_argument("--authorization", help="Optional explicit real-start-authorization.json path.")
    revoke_real_start_parser.add_argument("--reason", default="", help="Optional human revocation rationale.")
    revoke_real_start_parser.set_defaults(func=_revoke_real_start_command)

    resume_parser = subcommands.add_parser(
        "resume-preflight",
        help="Revalidate an approved preflight manifest and stop before real dispatch unless explicitly enabled.",
    )
    _add_common_config_arg(resume_parser)
    resume_parser.add_argument("--agent", required=True, help="Agent whose preflight should be resumed.")
    resume_parser.add_argument("--message-id", required=True, help="Message/task id to resume.")
    resume_parser.set_defaults(func=_resume_preflight_command)

    workflow_plan_parser = subcommands.add_parser(
        "workflow-plan",
        help="Run a D3 fake-adapter workflow preflight; dry-run only, no real agents or target writes.",
    )
    _add_common_config_arg(workflow_plan_parser)
    workflow_plan_parser.add_argument("--workflow-id", required=True, help="Workflow run id to create/resume.")
    workflow_plan_parser.add_argument("--step", required=True, help="Single fake preflight step id for the minimal D3 slice.")
    workflow_plan_parser.add_argument("--idempotency-key", required=True, help="Step idempotency key; duplicate succeeded keys do not redispatch.")
    workflow_plan_parser.add_argument("--target-root", help="Target/business repository root retained as audit context only.")
    workflow_plan_parser.add_argument("--dry-run", action="store_true", help="Required: D3 only records fake adapter preflight artifacts.")
    workflow_plan_parser.add_argument("--json", action="store_true", help="Emit JSON result.")
    workflow_plan_parser.set_defaults(func=_workflow_plan_command)

    workflow_status_parser = subcommands.add_parser(
        "workflow-status",
        help="Read D3 workflow preflight status from SQLite without mutation beyond schema init.",
    )
    _add_common_config_arg(workflow_status_parser)
    workflow_status_parser.add_argument("--workflow-id", required=True, help="Workflow run id to inspect.")
    workflow_status_parser.add_argument("--json", action="store_true", help="Emit JSON status.")
    workflow_status_parser.set_defaults(func=_workflow_status_command)

    git_plan_parser = subcommands.add_parser(
        "git-plan",
        help="Run a D4 Git Steward worktree/commit/PR proposal preflight; dry-run only.",
    )
    _add_common_config_arg(git_plan_parser)
    git_plan_parser.add_argument("--plan-id", required=True, help="Git Steward plan id to create/update.")
    git_plan_parser.add_argument("--task-id", required=True, help="Business/task id the Git proposal would serve.")
    git_plan_parser.add_argument("--target-root", required=True, help="Target/business repository root; retained as audit context only.")
    git_plan_parser.add_argument("--scope", required=True, help="Requested commit scope, e.g. apf_frontend or apf_backend.")
    git_plan_parser.add_argument("--changed-file", action="append", default=[], help="Relative changed file to include in the dry-run proposal; repeatable.")
    git_plan_parser.add_argument("--base-branch", help="Base branch to propose from; defaults to config.git.default_base_branch.")
    git_plan_parser.add_argument("--dry-run", action="store_true", help="Required: D4 only records proposals and never runs Git mutations.")
    git_plan_parser.add_argument("--json", action="store_true", help="Emit JSON result.")
    git_plan_parser.set_defaults(func=_git_plan_command)

    pr_gate_status_parser = subcommands.add_parser(
        "pr-gate-status",
        help="Read D4 PR gate status from SQLite without creating PRs, comments, pushes, or merges.",
    )
    _add_common_config_arg(pr_gate_status_parser)
    pr_gate_status_parser.add_argument("--plan-id", required=True, help="Git Steward plan id to inspect.")
    pr_gate_status_parser.add_argument("--json", action="store_true", help="Emit JSON status.")
    pr_gate_status_parser.set_defaults(func=_pr_gate_status_command)

    write_gate_parser = subcommands.add_parser(
        "write-gate-dry-run",
        help="Evaluate a Phase D1 dry-run-only write gate and write an audit artifact without target writes.",
    )
    _add_common_config_arg(write_gate_parser)
    write_gate_parser.add_argument("--project-root", help="Legacy project root for resolving target-context defaults; do not use for Orchestrator artifacts.")
    write_gate_parser.add_argument(
        "--orchestrator-root",
        help="Explicit AI Workgroup Orchestrator root for audit/rollback/idempotency artifacts.",
    )
    write_gate_parser.add_argument(
        "--target-root",
        help="Explicit target/business repository root for candidate path validation; never used for Orchestrator artifacts.",
    )
    write_gate_parser.add_argument("--candidate", required=True, help="Candidate write intent JSON path.")
    write_gate_parser.add_argument("--envelope", help="Approval envelope JSON path; omit to force deny.")
    write_gate_parser.add_argument("--json", action="store_true", help="Emit JSON result.")
    write_gate_parser.add_argument(
        "--fail-on-deny",
        action="store_true",
        help="Return exit code 3 when evaluation completes with decision=deny.",
    )
    write_gate_parser.set_defaults(func=_write_gate_dry_run_command)

    return parser


def _print_doctor_result(result: DoctorResult) -> None:
    status = "OK" if result.ok else "FAILED"
    print(f"AIWG doctor: {status}")
    for message in result.messages:
        print(f"[OK] {message}")
    for warning in result.warnings:
        print(f"[WARN] {warning}")
    for error in result.errors:
        print(f"[ERROR] {error}")


def _doctor_command(args: argparse.Namespace) -> int:
    result = run_doctor(config_path=Path(args.config), project_root=Path(args.project_root))
    _print_doctor_result(result)
    return 0 if result.ok else 1


def _init_config_command(args: argparse.Namespace) -> int:
    path = write_default_config(Path(args.config), project_root=Path("."), overwrite=args.force)
    print(f"Wrote default Phase A0 config: {path}")
    return 0


def _load_config_and_project_root(config_path: str) -> tuple[dict, Path]:
    path = Path(config_path)
    config = load_config(path)
    return config, resolve_project_root(config, config_path=path)


def _resolve_cli_orchestrator_root(
    *,
    config: dict,
    config_path: Path,
    override: str | None,
) -> Path:
    """Resolve D1 audit roots from CLI config context, not target project_root."""
    if override:
        return Path(override)
    configured = config.get("orchestrator_root")
    if configured:
        configured_path = Path(str(configured))
        if configured_path.is_absolute():
            return configured_path
        return config_path.parent / configured_path
    return config_path.parent


def _init_db_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    db_path = init_database(config=config, project_root=project_root)
    print(f"Initialized SQLite database: {db_path}")
    print("schema_migrations: version=1,2,3,4,5,6,7,8,9")
    print("PRAGMA: journal_mode=WAL foreign_keys=ON busy_timeout>=5000")
    return 0


def _import_inbox_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    try:
        result = import_inbox(
            config=config,
            project_root=project_root,
            agent=args.agent,
            dry_run=bool(args.dry_run),
            manifest_path=Path(args.manifest) if args.manifest else None,
            evidence_only=bool(args.evidence_only),
            approval_artifact_path=Path(args.approval_artifact) if args.approval_artifact else None,
        )
    except ValueError as exc:
        print(f"import-inbox: error={exc}")
        return 2
    print(
        "import-inbox: "
        f"agent={args.agent or '*'} "
        f"dry_run={result.dry_run} "
        f"scanned={result.scanned} "
        f"valid={result.valid} "
        f"invalid={result.invalid} "
        f"imported={result.imported} "
        f"skipped_existing={result.skipped_existing} "
        f"manifest={result.manifest_path or '-'} "
        f"evidence_only={result.evidence_only}"
    )
    for invalid in result.invalid_messages:
        print(f"ERR {invalid.path}")
        for error in invalid.errors:
            print(f"  - {error}")
    return 0 if result.invalid == 0 else 1


def _list_tasks_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    tasks = list_tasks(config=config, project_root=project_root, status=args.status, agent=args.agent)
    if not tasks:
        print("No tasks.")
        return 0
    print("id\ttask\tstatus\tto\tfrom\tpath")
    for task in tasks:
        print(
            f"{task['id']}\t{task['task_id']}\t{task['status']}\t"
            f"{task['to_agent']}\t{task['from_agent']}\t{task['message_path']}"
        )
    return 0


def _status_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    snapshot = get_status_snapshot(
        config=config,
        project_root=project_root,
        recent_events=args.recent_events,
        task_limit=args.task_limit,
        status=args.status,
        agent=args.agent,
    )
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(render_status_text(snapshot))
    return 0


def _role_health_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    snapshot = get_role_health_snapshot(config=config, project_root=project_root)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(render_role_health_text(snapshot))
    return 0


def _role_health_snapshot_command(args: argparse.Namespace) -> int:
    return _role_health_command(args)


def _external_review_gate_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    snapshot = get_external_review_gate_snapshot(config=config, project_root=project_root)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(render_external_review_gate_text(snapshot))
    return 0


def _workflow_contract_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    snapshot = get_workflow_contract_snapshot(
        config=config,
        project_root=project_root,
        topology_path=Path(args.topology) if args.topology else None,
        workflow_path=Path(args.workflow) if args.workflow else None,
    )
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(render_workflow_contract_text(snapshot))
    return 0 if (snapshot.get("validation") or {}).get("passed") else 1


def _d5_preflight_command(args: argparse.Namespace) -> int:
    if not bool(args.dry_run):
        print("d5-preflight: error=--dry-run is required for D5 fake/dry-run preflight")
        return 2
    config, project_root = _load_config_and_project_root(args.config)
    try:
        snapshot = evaluate_d5_preflight(
            config=config,
            project_root=project_root,
            workflow_id=args.workflow_id,
            target_root=Path(args.target_root),
            dry_run=True,
            include_d5_1=bool(getattr(args, "include_d5_1", False)),
            external_review_fixture=(
                Path(args.external_review_fixture) if getattr(args, "external_review_fixture", None) else None
            ),
        )
    except ValueError as exc:
        print(f"d5-preflight: error={exc}")
        return 2
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(render_d5_preflight_text(snapshot))
    if snapshot.get("status") == "blocked" and bool(getattr(args, "fail_on_blocked", False)):
        return 3
    return 0 if snapshot.get("status") in {"passed_dry_run", "blocked"} else 1


def _adapter_readiness_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    db_path = init_database(config=config, project_root=project_root)
    report = write_adapter_binary_readiness_report(
        config=config,
        project_root=project_root,
        db_path=db_path,
        run_version_probes=not bool(args.no_version_probe),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report.get("summary") or {}
        print(
            "adapter-readiness: "
            "status=checked "
            f"adapters={summary.get('total', 0)} "
            f"available={summary.get('available', 0)} "
            f"missing={summary.get('missing', 0)} "
            f"ready={summary.get('ready', 0)} "
            f"report={report.get('report_path')}"
        )
    return 0


def _legacy_audit_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    result = legacy_audit(config=config, project_root=project_root)
    print(
        "legacy audit: "
        f"mode={result.mode} "
        f"scanned={result.scanned} "
        f"valid={result.valid} "
        f"invalid={result.invalid} "
        f"imported={result.imported}"
    )
    if result.report_path is not None:
        print(f"report={result.report_path}")
    for invalid in result.invalid_messages:
        print(f"ERR {invalid.path}")
        for error in invalid.errors:
            print(f"  - {error}")
    return 0


def _run_once_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    result = run_once(config=config, project_root=project_root, agent=args.agent)
    print(
        "run-once: "
        f"agent={result.agent} "
        f"status={result.status} "
        f"message_id={result.message_id or '-'} "
        f"imported={result.import_result.imported} "
        f"skipped_existing={result.import_result.skipped_existing} "
        f"staled={result.stale_result.staled}"
    )
    if result.report_path is not None:
        print(f"report={result.report_path}")
    if result.manifest_path is not None:
        print(f"manifest={result.manifest_path}")
    if result.error:
        print(f"error={result.error}")
    return 1 if result.status == "failed" else 0


def _approve_preflight_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    result = approve_preflight(
        config=config,
        project_root=project_root,
        agent=args.agent,
        message_id=args.message_id,
        operator=args.operator,
        manifest_path=Path(args.manifest) if args.manifest else None,
        ttl_minutes=args.ttl_minutes,
        reason=args.reason,
    )
    print(
        "approve-preflight: "
        f"status={result.status} "
        f"message_id={result.message_id} "
        f"agent={args.agent} "
        f"approval_id={result.approval_id or '-'}"
    )
    if result.manifest_path is not None:
        print(f"manifest={result.manifest_path}")
    if result.manifest_sha256 is not None:
        print(f"manifest_sha256={result.manifest_sha256}")
    if result.expires_at is not None:
        print(f"expires_at={result.expires_at}")
    if result.error:
        print(f"error={result.error}")
    return 0 if result.status == "approved" else 1


def _approve_real_start_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    result = approve_real_start(
        config=config,
        project_root=project_root,
        agent=args.agent,
        message_id=args.message_id,
        operator=args.operator,
        sandbox_plan_path=Path(args.sandbox_plan),
        sandbox_report_path=Path(args.sandbox_report),
        ttl_minutes=args.ttl_minutes,
        reason=args.reason,
    )
    print(
        "approve-real-start: "
        f"status={result.status} "
        f"message_id={result.message_id} "
        f"agent={args.agent} "
        f"approval_id={result.approval_id or '-'}"
    )
    if result.authorization_path is not None:
        print(f"authorization={result.authorization_path}")
    if result.manifest_path is not None:
        print(f"manifest={result.manifest_path}")
    if result.sandbox_plan_path is not None:
        print(f"sandbox_plan={result.sandbox_plan_path}")
    if result.sandbox_report_path is not None:
        print(f"sandbox_report={result.sandbox_report_path}")
    if result.expires_at is not None:
        print(f"expires_at={result.expires_at}")
    if result.error:
        print(f"error={result.error}")
    return 0 if result.status == "authorized" else 1


def _revoke_real_start_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    result = revoke_real_start(
        config=config,
        project_root=project_root,
        agent=args.agent,
        message_id=args.message_id,
        operator=args.operator,
        authorization_path=Path(args.authorization) if args.authorization else None,
        reason=args.reason,
    )
    print(
        "revoke-real-start: "
        f"status={result.status} "
        f"message_id={result.message_id} "
        f"agent={args.agent} "
        f"approval_id={result.approval_id or '-'}"
    )
    if result.authorization_path is not None:
        print(f"authorization={result.authorization_path}")
    if result.revoked_at is not None:
        print(f"revoked_at={result.revoked_at}")
    if result.error:
        print(f"error={result.error}")
    return 0 if result.status == "revoked" else 1


def _resume_preflight_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    result = resume_preflight(
        config=config,
        project_root=project_root,
        agent=args.agent,
        message_id=args.message_id,
    )
    print(
        "resume-preflight: "
        f"status={result.status} "
        f"message_id={result.message_id} "
        f"agent={args.agent} "
        f"approval_id={result.approval_id or '-'}"
    )
    if result.manifest_path is not None:
        print(f"manifest={result.manifest_path}")
    if result.run_id is not None:
        print(f"run_id={result.run_id}")
    if result.report_path is not None:
        print(f"report={result.report_path}")
    if result.sandbox_plan_path is not None:
        print(f"sandbox_plan={result.sandbox_plan_path}")
    if result.stdout_path is not None:
        print(f"stdout={result.stdout_path}")
    if result.stderr_path is not None:
        print(f"stderr={result.stderr_path}")
    if result.error:
        print(f"error={result.error}")
    return 0 if result.status in {
        "real_dispatch_blocked",
        "dry_run_succeeded",
        "adapter_output_done",
        "adapter_output_needs_revision",
        "sandbox_invocation_ready",
        "sandbox_process_succeeded",
        "sandbox_process_failed",
        "sandbox_process_timed_out",
        "sandbox_process_blocked",
    } else 1


def _workflow_plan_command(args: argparse.Namespace) -> int:
    if not bool(args.dry_run):
        print("workflow-plan: error=--dry-run is required for D3 fake-adapter preflight")
        return 2
    config, project_root = _load_config_and_project_root(args.config)
    step = {
        "step_id": args.step,
        "adapter": "fake",
        "idempotency_key": args.idempotency_key,
        "target_root": args.target_root or "",
        "candidate_paths": [],
    }
    try:
        result = plan_workflow_dry_run(
            config=config,
            project_root=project_root,
            workflow_id=args.workflow_id,
            steps=[step],
        )
    except ValueError as exc:
        print(f"workflow-plan: error={exc}")
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "workflow-plan: "
            f"workflow_id={result.workflow_id} "
            f"status={result.status} "
            f"dispatched_steps={result.dispatched_steps} "
            f"real_agents_started={result.real_agents_started} "
            f"target_writes_performed={result.target_writes_performed} "
            f"mcp_mutation_tools_exposed={result.mcp_mutation_tools_exposed}"
        )
        if result.duplicate_idempotency_key:
            print(f"duplicate_idempotency_key={result.duplicate_idempotency_key}")
        if result.error:
            print(f"error={result.error}")
    return 0 if result.status in {"completed", "duplicate_idempotency_key"} else 1


def _workflow_status_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    try:
        status = get_workflow_status(config=config, project_root=project_root, workflow_id=args.workflow_id)
    except ValueError as exc:
        print(f"workflow-status: error={exc}")
        return 1
    if args.json:
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "workflow-status: "
            f"workflow_id={status.workflow_id} "
            f"status={status.status} "
            f"dry_run={status.dry_run} "
            f"last_successful_step_id={status.last_successful_step_id or '-'}"
        )
        for step in status.steps:
            print(
                "step: "
                f"step_id={step['step_id']} "
                f"status={step['status']} "
                f"idempotency_key={step['idempotency_key']} "
                f"output_status={step['output_status']}"
            )
    return 0


def _git_plan_command(args: argparse.Namespace) -> int:
    if not bool(args.dry_run):
        print("git-plan: error=--dry-run is required for D4 Git Steward preflight")
        return 2
    config, project_root = _load_config_and_project_root(args.config)
    try:
        result = plan_git_dry_run(
            config=config,
            project_root=project_root,
            plan_id=args.plan_id,
            task_id=args.task_id,
            target_root=Path(args.target_root),
            requested_scope=args.scope,
            changed_files=list(args.changed_file or []),
            base_branch=args.base_branch,
        )
    except ValueError as exc:
        print(f"git-plan: error={exc}")
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "git-plan: "
            f"plan_id={result.plan_id} "
            f"status={result.status} "
            f"dry_run={result.dry_run} "
            f"target_writes_performed={result.target_writes_performed} "
            f"git_commit_performed={result.git_commit_performed} "
            f"git_push_performed={result.git_push_performed} "
            f"git_merge_performed={result.git_merge_performed} "
            f"mcp_mutation_tools_exposed={result.mcp_mutation_tools_exposed}"
        )
        if result.artifact_path is not None:
            print(f"artifact={result.artifact_path}")
        if result.denied_reasons:
            print("denied_reasons=" + ",".join(result.denied_reasons))
    return 0 if result.status in {"planned", "no_candidate_changes"} else 1


def _pr_gate_status_command(args: argparse.Namespace) -> int:
    config, project_root = _load_config_and_project_root(args.config)
    try:
        status = get_pr_gate_status(config=config, project_root=project_root, plan_id=args.plan_id)
    except ValueError as exc:
        print(f"pr-gate-status: error={exc}")
        return 1
    if args.json:
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "pr-gate-status: "
            f"plan_id={status.plan_id} "
            f"gate_state={status.gate_state} "
            f"required_checks_state={status.required_checks_state} "
            f"review_threads_state={status.review_threads_state} "
            f"read_only={status.read_only}"
        )
    return 0 if status.gate_state != "not_found" else 1


def _write_gate_dry_run_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = load_config(config_path)
    project_root = Path(args.project_root) if args.project_root else resolve_project_root(config, config_path=config_path)
    cli_orchestrator_root = _resolve_cli_orchestrator_root(
        config=config,
        config_path=config_path,
        override=args.orchestrator_root,
    )
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8")) if args.envelope else None
    result = evaluate_write_gate_dry_run(
        config=config,
        project_root=project_root,
        candidate_intent=candidate,
        approval_envelope=envelope,
        orchestrator_root=cli_orchestrator_root,
        target_root=Path(args.target_root) if args.target_root else None,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "write-gate-dry-run: "
            f"decision={result.decision} "
            f"target_writes_performed={result.target_writes_performed} "
            f"duplicate_idempotency_key={result.duplicate_idempotency_key} "
            f"audit_artifact_path={result.audit_artifact_path}"
        )
        if result.reasons:
            print("reasons=" + ",".join(result.reasons))
    if args.fail_on_deny and result.decision == "deny":
        return 3
    return 0


def _validate_message_command(args: argparse.Namespace) -> int:
    # Phase A1 validation is read-only, but still loads the project config so a
    # misspelled --config path is caught early and CLI calls stay uniform.
    load_config(Path(args.config))

    results = [validate_message_file(Path(path)) for path in args.paths]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "Path": str(result.path) if result.path is not None else None,
                        "Valid": result.valid,
                        "Errors": result.errors,
                    }
                    for result in results
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for result in results:
            path = result.path if result.path is not None else ""
            if result.valid:
                print(f"OK  {path}")
            else:
                print(f"ERR {path}")
                for error in result.errors:
                    print(f"  - {error}")
    return 0 if all(result.valid for result in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

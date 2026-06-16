---
from: Hermes
to: Human
type: acceptance-summary
task: AIWG-Phase-C-readonly-MCP
status: conditionally-accepted
created_at: 2026-06-06T14:27:05+08:00
---

# Phase C read-only MCP acceptance summary

## Conclusion

Phase C read-only MCP is **conditionally accepted** as a `ready_for_review` read-only control-plane package.

It is **not ready_for_real_agent_execution**.

Current precise status:

```text
server ready: yes
read-only control-plane: yes
acceptance pack: yes
Hermes native MCP client E2E smoke completed: yes
real-agent execution approved: no
```

The Phase C result proves that the local AI Workgroup Orchestrator MCP server can expose read-only SQLite control-plane state through these tools:

```text
status
list_tasks
get_task
recent_events
```

The Phase C E2E follow-up proves that the active default Hermes profile can load the `aiwg_readonly` MCP server and invoke the live Hermes MCP tools. Evidence is stored at:

```text
docs/ai-workgroup/state/artifacts/phase-c-hermes-mcp-e2e/e2e-smoke.json
```

The E2E smoke used:

```text
hermes mcp list
hermes mcp test aiwg_readonly
Hermes native MCP registry dispatch: mcp_aiwg_readonly_status
Hermes native MCP registry dispatch: mcp_aiwg_readonly_list_tasks
Hermes CLI one-shot chat tool call: mcp_aiwg_readonly_status
```

It does **not** authorize real agents or any mutation path.

## Safety boundary

Phase C keeps all execution and mutation paths closed:

```text
allow_write=false
allow_real_agents=false
allow_real_adapter_dispatch=false
allow_real_process_execution=false
allow_push=false
allow_merge=false
allow_deploy=false
allow_modify_codex_automations=false
```

MCP mutation tools remain forbidden. The MCP server must not expose claim, write, approve, dispatch, start-real-agent, push, merge, deploy, or protected-business-repository-write actions.

## CodeX review remediation notes

CodeX's conditional acceptance is reflected here:

1. `151 passed` was observed for the final Phase C/C5 suite.
2. The MCP server exposes only `status`, `list_tasks`, `get_task`, and `recent_events`.
3. `status` reports `read_only=true` and `mutation_actions=[]`.
4. stale adapter readiness warning exists and includes `blocks_real_agent_start=true`.
5. No real-agent authorization, preflight approval, or operator approval has been granted.
6. APF3b direct source/doc/test/report files were not restored into AIVideoTrans.

Important scope clarification: `remaining_count=0 excludes __pycache__/*.pyc` and only refers to APF3b source, docs, tests, and report files. A read-only scan found 6 APF3b `.pyc` residues under AIVideoTrans `__pycache__` directories. Cleanup was not performed because deleting files in the protected business repository requires explicit user authorization.

## Stable documentation vs runtime artifacts

The machine-readable Phase C acceptance pack remains in the runtime evidence area:

```text
docs/ai-workgroup/state/artifacts/phase-c-readonly-mcp-acceptance/acceptance.json
```

This stable summary is intentionally stored outside `state/artifacts`:

```text
docs/guides/phase-c-readonly-mcp-acceptance-summary.md
```

Use this page as the long-term review pointer; use `state/artifacts` for detailed run evidence.

## Hermes native MCP E2E evidence

Hermes native MCP client E2E smoke completed:

- `aiwg_readonly` was added to the active default Hermes profile through `hermes mcp add`;
- `sampling.enabled=false` was set with `hermes config set`;
- `hermes mcp list` showed `aiwg_readonly` enabled;
- `hermes mcp test aiwg_readonly` connected and discovered 4 business tools;
- native registry dispatch successfully invoked `mcp_aiwg_readonly_status` and `mcp_aiwg_readonly_list_tasks`;
- one-shot `hermes chat -q ... --cli --verbose` loaded `mcp_aiwg_readonly_status`, called it, and returned:

```json
{"read_only":true,"mutation_actions":[],"warning_codes":["adapter_readiness_stale"]}
```

The config was retained after smoke because the server is read-only; a timestamped config backup exists for rollback.

## Recommended next phase

Phase D0 controlled write-gate design only.

Phase D0 should define and test the deterministic write gate and approval envelope while keeping real writes disabled. It should not enable real agents, should not expose MCP mutation tools, and should not modify AIVideoTrans business files.

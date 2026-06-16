---
from: Hermes
to: Human
type: checklist
task: AIWG-Phase-C-Hermes-Desktop-MCP-client-smoke
status: pending-authorization
created_at: 2026-06-06T14:27:05+08:00
---

# Hermes Desktop MCP client smoke checklist

This checklist requires explicit user authorization before execution because it may modify or temporarily load Hermes profile configuration.

Do not modify ~/.hermes/config.yaml without authorization.

## Scope

Goal: prove the live Hermes Desktop/CLI MCP client can load `aiwg_readonly` and call read-only AI Workgroup Orchestrator tools.

This is different from server-only smoke testing. Phase C already proved that `python -m aiwg.mcp.server` runs and exposes read-only tools; this checklist proves Hermes client discovery and actual tool invocation.

## Preconditions

- Do not enable real agents.
- Do not modify AIVideoTrans.
- No mutation tool may be exposed.
- Do not add secrets, tokens, credentials, or connection strings to MCP config. Use `[REDACTED]` if a placeholder is necessary.
- Preserve `allow_write=false` and `allow_real_agents=false`.

## Proposed config block

Use the existing project-local example:

```text
D:/AIGroup/ai-workgroup-orchestrator/docs/examples/hermes-mcp-aiwg-readonly.yaml
```

Expected server name:

```text
aiwg_readonly
```

## Execution steps after authorization

1. Record whether `~/.hermes/config.yaml` exists.
2. Create a backup before editing or temporary loading:

```bash
cp ~/.hermes/config.yaml ~/.hermes/config.yaml.aiwg-mcp-smoke.bak
```

If the file does not exist, record that and create only the minimal authorized config fragment.

3. Add or temporarily load the `mcp_servers.aiwg_readonly` block.
4. Restart Hermes Desktop/CLI or use `/reload-mcp` if running in an interactive Hermes session.
5. Run:

```bash
hermes mcp list
hermes mcp test aiwg_readonly
```

6. Confirm expected discovered tools:

```text
mcp_aiwg_readonly_status
mcp_aiwg_readonly_list_tasks
mcp_aiwg_readonly_get_task
mcp_aiwg_readonly_recent_events
```

7. Perform actual Hermes Desktop/CLI tool call to `mcp_aiwg_readonly_status`.
8. Perform actual Hermes Desktop/CLI tool call to `mcp_aiwg_readonly_list_tasks`.
9. Confirm responses include:

```text
status
list_tasks
read_only=true
mutation_actions=[]
```

10. Confirm forbidden tools are absent:

```text
claim_message
write_message
update_status
assign_task
record_decision
approve
start_real_agent
run_real_agent
push
merge
deploy
```

11. Restore config if the smoke was intended to be temporary:

```bash
cp ~/.hermes/config.yaml.aiwg-mcp-smoke.bak ~/.hermes/config.yaml
```

12. Restart Hermes Desktop/CLI or use `/reload-mcp` again after restore.

## Acceptance criteria

- `hermes mcp list` shows `aiwg_readonly`.
- `hermes mcp test aiwg_readonly` succeeds.
- Actual Hermes Desktop/CLI tool call to `mcp_aiwg_readonly_status` succeeds.
- Actual Hermes Desktop/CLI tool call to `mcp_aiwg_readonly_list_tasks` succeeds.
- Returned payloads preserve `read_only=true` and `mutation_actions=[]`.
- No mutation tool may be exposed.
- No AIVideoTrans file is modified.
- No real agent is started.
- If a backup was created, restore behavior is recorded.

## Current status

Pending. This checklist documents the smoke but does not execute it.

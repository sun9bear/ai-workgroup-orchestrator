# Phase D5.3.3 — protected_target_roots config/schema/guide + parser hardening

状态：`implemented_ready_for_verification`

## 1. 目标

D5.3.3 将 `protected_target_roots` 从测试/acceptance 中的隐式约定提升为正式配置契约，并收紧 `aiwg.evidence_paths.protected_target_roots_from_config()` 的解析形态，避免未来误配置被静默解释为空保护集。

## 2. Config contract

`protected_target_roots` 是顶层 config key，用于声明业务仓/target repository 根目录。任何 D5 evidence/artifact/state writer 在使用该配置时，都必须把这些路径视为 protected roots：写入根或最终写入路径不得与其中任何 root 发生任一方向 overlap。

默认通用配置使用空列表：

```yaml
protected_target_roots: []
```

本项目运行配置应显式列出 AIVideoTrans 业务仓：

```yaml
protected_target_roots:
  - D:/example/protected-business-repo
```

## 3. Parser schema contract

`protected_target_roots_from_config(config)` 只接受以下合法形态：

- `single string/path`：单个 string 或 `pathlib.Path`；
- `list/tuple of string/path`：由 string 或 `pathlib.Path` 组成的一层 list/tuple；
- 空 list/tuple 表示当前没有配置 protected target roots；
- 缺失该 key 时保持向后兼容，返回空 tuple。

解析结果统一为 `tuple[pathlib.Path, ...]`。

非法形态必须 `fail-closed`，抛出包含 `protected_target_roots` 的 `ValueError`，不得静默返回空 tuple 或把畸形项转换成路径。拒绝范围包括：

- `reject dict`：dict/object mapping；
- `reject number`：int/float 等数字；
- `reject bool`：`true` / `false`；
- `reject object`：任意非 path-like object；
- `reject nested list`：嵌套 list/tuple；
- `blank string`：空字符串或全空白字符串；
- list/tuple 内的 `None`、数字、bool、object、嵌套 list、blank string。

## 4. Guard interaction

D5.3.1/D5.3.2 已经让以下入口使用 `protected_target_roots_from_config()`：

- adapter preflight artifact writer；
- fake adapter artifact writer；
- verification artifact writer；
- adapter binary readiness artifact writer；
- legacy audit report writer；
- `state_db` config entrypoint `resolve_db_path()`。

因此 D5.3.3 parser hardening 会让畸形 `protected_target_roots` 在这些入口处 fail-closed，而不是让 guard 误以为没有 protected roots。

## 5. 非目标 / 禁止项

本切片仍只做配置契约和 parser hardening，不扩大执行面：

- 不启用 real agents；
- 不开放 MCP mutation tools；
- 不写 AIVideoTrans 业务仓；
- 不碰 GitHub write / PR mutation / PR comment；
- 不部署；
- 不修改 CodeX Automation；
- 不做 shared-helper 大重构；
- 不暴露裸 `connect_database(path)` 给 CLI/MCP/external adapter。

## 6. 验证要求

- D5.3.3 targeted parser/config/guide tests 通过；
- D5.3.1 + D5.3.2 boundary suite 仍通过；
- state/database regression 仍通过；
- full AIWG suite 仍通过；
- `doctor` 仍为 `AIWG doctor: OK`；
- MCP surface 仍只有 `status / list_tasks / get_task / recent_events`；
- AIVideoTrans 中不得出现 D5.3.3 marker 或新的 sqlite artifact。

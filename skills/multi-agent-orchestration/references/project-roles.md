# 项目岗位

只在需要为某个项目固定模型、推理档位、候选顺序或补充规则时阅读。没有项目配置时，技能使用全局岗位 `orch_<role>`（由 `roles.py install-global` 安装，继承全局模型和档位）。

## 配置文件 `.codex/orchestration.json`

```json
{
  "schema": "multi-agent-orchestration/v1",
  "state_dir": ".orchestration",
  "protected_paths": ["data/raw"],
  "startup": {"read": ["AGENTS.md"], "preflight": {"command": "python -m unittest", "result": ".orchestration/preflight.txt"}},
  "rules": ["data/raw 下的原始资料只读"],
  "max_live": 3,
  "dispatch": "named",
  "agent_prefix": "proj",
  "roles": {
    "worker": {"candidates": [
      {"agent": "proj_worker", "priority": 1, "model": "example-model-a", "reasoning_effort": "max-available"},
      {"agent": "orch_worker", "priority": 2}
    ]},
    "reviewer": {"candidates": [{"agent": "orch_reviewer", "priority": 1}]}
  }
}
```

- `schema` 必填。同路径文件没有这个值时脚本直接报错：那是项目自有的编排配置，应使用项目自带的技能。
- 其余键都可省略：
  - `state_dir` 默认 `.orchestration`，必须在项目内且不是项目根；
  - `protected_paths` 追加到默认保护集（状态目录、`.git`、`.codex`、`.agents`、`.env`）；
  - `startup.read` 是主控启动时必读的文件；`startup.preflight` 由主控每轮运行一次，结果作为合同 inputs；
  - `rules` 写入每份合同的 `project_rules`，也写进托管岗位的指令；
  - `max_live` 只能调低原生并发上限；
  - `dispatch` 为 `auto`（默认）或 `named`；有 `roles` 时只能是 `named`；
  - `agent_prefix` 默认 `proj`，不能是 `orch`。
- 校验会拒绝未知键、重复 JSON 键、同岗重复候选，以及用布尔值冒充整数。

## 候选

`roles.<role>.candidates[]` 必填 `agent` 和 `priority`（0–10 的整数，1 最高，0 停用）。

- **托管候选**：名称为 `<prefix>_<role>` 或 `<prefix>_<role>_<变体>`（变体为小写字母数字，可用下划线分段）。可带 `model`、`reasoning_effort`（具体档位或 `max-available`，必须同时给 `model`）和 `instructions`。由 `roles.py generate` 生成岗位文件。
- **引用候选**：其他名称，指向全局 `orch_*` 或手写的岗位，不能带 `model`、`reasoning_effort`、`instructions`。

## 选择语义

每个任务在 `add-task` 时选择一次，`start` 时复核未漂移：

- 有 `roles`：只在该岗位的启用候选中，按 priority（同值按书写顺序）取第一个**已加载**的候选，以 named 派发。`requested_agent` 只能在启用候选中挑选，不能绕过 priority 0。没有可用候选就报告缺口并停止，不降级、不回退到全局岗位。托管候选的岗位文件必须与当前配置一致（`doctor` 显示 current）。
- 没有 `roles`：用全局 `orch_<role>`。已加载则 named；未加载且 `dispatch=auto` 时改为 inline（默认子代理，合同携带完整岗位职责，记录 `degraded_reason` 并告知用户）；未加载且 `dispatch=named` 时停止。
- 「已加载」以主控从当前会话派发工具的 `agent_type` 参数说明中抄录的名称为准，不用磁盘文件列表代替。
- 配置文件、岗位文件、岗位模板或所选模型的目录条目任一变化，`start` 都会拒绝；从未开始过的任务可以 `rebind`。

## 生成与漂移

```powershell
python -B -X utf8 <skill>/scripts/roles.py generate --root .
python -B -X utf8 <skill>/scripts/roles.py doctor --root .
```

- 生成文件 `<项目>/.codex/agents/<agent>.toml`：`developer_instructions` = 岗位模板 + 项目 rules + 候选 instructions。只读岗另写 `sandbox_mode = "read-only"`；托管候选带 `model` 与 `model_reasoning_effort`。
- 文件头两行是生成标记和正文哈希。`generate` 只覆盖带标记且未被手改的文件；手改或外来文件拒绝覆盖；从配置中移除的托管文件只报告不删除。
- `doctor` 的状态：`missing`（不存在）、`current`（与期望一致）、`outdated`（未手改但与当前模板或配置不同）、`modified`（带标记但被手改）、`foreign`（不是本技能生成）。
- 生成或更新后需要新开会话才会加载，然后重新抄录已加载岗位并 `set-loaded-agents`。
- 写项目 `.codex/` 与全局 `$CODEX_HOME/agents/` 前先请求用户批准。

## 模型目录与推理档位

- 从生效配置读取 `model_catalog_json`：先看项目 `.codex/config.toml`，再看 `$CODEX_HOME/config.toml`；相对路径按所在配置文件的目录解析。
- 按 `models[].slug` 校验模型、按 `supported_reasoning_levels[].effort` 校验档位。
- `max-available`：目录声明了 `max` 就用 `max`；否则按 none < minimal < low < medium < high < xhigh < max < ultra 取已声明的最高档；遇到未知档名报错。
- 未配置模型目录时仍可生成，但记为 `catalog_verified:false`，且不能用 `max-available`。
- 账本只固定所选模型目录条目的规范化哈希；条目变化会让 start 拒绝。
- 目录声明和岗位文件只是期望配置，不证明后端实际使用了该模型或档位。

## 第三方模型的上下文排查

通过网关接入的第三方模型，上下文窗口和压缩行为可能与目录声明不同：

- 先核对目录条目中的 `context_window` 与 `auto_compact_token_limit`，估算合同、输入文件和预期输出的体量。
- 合同保持精简：只列必要的 inputs 和范围，大文件给路径和哈希，不在消息里转抄。
- 出现输出截断、重复压缩、回复丢失字段等异常时，先缩小任务（拆分文件或步骤），或在配置中换用其他候选；返工仍受两次尝试上限约束。
- 不自动修改全局配置、网关路由或模型目录；需要调整时向用户说明现象和建议。

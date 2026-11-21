---
name: multi-agent-orchestration
description: 在用户明确要求多代理/子代理分工、并行实施或独立审查时，由主控按岗位（worker、engineer、reviewer、researcher、editor）派发 Codex 子代理，以 SQLite 账本记录合同、文件所有权与验收；可为单个项目生成专用岗位和模型候选。适用于任意项目；项目自带编排技能时以项目为准。Multi-agent orchestration, subagents, contracts, ledger.
license: MIT
---

# 多代理编排

主控（当前会话）拆分任务、写合同、派发子代理、独立验收；子代理只按合同执行。账本 `scripts/journal.py` 记录每一步，岗位工具 `scripts/roles.py` 管理岗位定义。脚本只用 Python ≥3.11 标准库，下文 `<skill>` 指本技能目录，命令都在项目根执行：

```powershell
python -B -X utf8 <skill>/scripts/journal.py --root . <命令> --input <JSON 文件>
python -B -X utf8 <skill>/scripts/roles.py doctor --root .
```

JSON 输入用 UTF-8 文件或 stdin 传入，不要把长文本拼进命令行。合同和交付的完整格式见 [合同与交付](references/contracts.md)；项目专用岗位见 [项目岗位](references/project-roles.md)。

## 1. 适用条件

- 只在用户明确要求多代理、子代理分工、并行实施或独立审查时使用。
- 主控能直接完成的小任务直接做，不为形式拆分。
- 项目自带编排技能（例如 `.codex/orchestration.json` 没有本技能的 `schema`）时以项目为准，本技能脚本会拒绝该配置。

## 2. 启动

1. 读项目 `AGENTS.md`，以及项目配置 `startup.read` 列出的文件。
2. 运行 `journal.py config` 和 `roles.py doctor --root .`，确认状态目录、并发上限、保护路径、岗位状态和模型目录。
3. 配置了 `startup.preflight` 时，主控本轮运行一次该命令，把结果写到 `result` 路径，作为合同 inputs 引用；子代理不重复运行。
4. 记录已加载岗位：从当前会话原生派发工具（`spawn_agent`）的 `agent_type` 参数说明中抄录可选的精确岗位名；没有该参数时记为空数组。不得用磁盘上的岗位文件列表代替。逐项抄录说明中出现的全部名称，不要只挑与本轮任务相关的；提交前与说明逐一核对，漏抄会导致本可具名派发的任务被降级。
5. `create-run`：`{"run_id", "objective", "loaded_agents": {"observed_from": "抄录依据", "names": [...]}}`。会话变化（新开会话、岗位重新加载）后用 `set-loaded-agents` 更新。

## 3. 主循环

1. **登记**：`add-task` 写入合同（单个或 `{"tasks":[...]}` 批量，批量原子执行）。账本自动选择岗位并写入 `dispatch`、`project_rules`、`baseline_calls`；inline 时另写 `role_instructions`。返回 `notice` 时把降级原因告诉用户。
2. **开始**：`start` 检查依赖已 accepted、并发名额、写入范围不重叠、inputs 哈希和岗位选择未漂移，导出不可覆盖的合同文件，返回绝对路径、SHA-256 和派发消息 `message`。
3. **派发**：用原生工具派发，消息只含 `message` 的内容（岗位、run/task/attempt、合同路径和哈希、核对指令），不转抄合同正文。
   - named 传 `agent_type=dispatch.agent_type`；inline 完全省略 `agent_type`（不要填 `default` 等占位值），`bind` 中如实记录实际传入的值。
   - 不继承上下文：v1 `fork_context=false`，v2 `fork_turns="none"`。
   - 不传 `model`、`reasoning_effort`。
   - 派发被拒且没有拿到 Agent 句柄时用 `cancel-start`（不计尝试）。
4. **绑定**：`bind` 记录 Agent 句柄和实际派发参数 `spawn{tool_version, agent_type, inherits_context, model_override, reasoning_effort_override}`；每次尝试只能绑定一次。
5. **回收**：用会话实际提供的等待工具收齐结果，把回复原文交给账本：先 `check-report`，再 `report`（`reply` 或 `reply_path`）。账本保存每版原文。交付不一致时用补充消息请同一代理更正一次（不计新尝试）；第二次仍不一致自动记为 failed。
6. **释放**：关闭代理并确认它已终止（关闭工具的返回或状态为依据），再 `release`（`terminal_confirmed:true` 和依据）。release 前写入范围和并发名额一直占用。
7. **验收**：主控独立复核产物（读文件、跑测试、看差异），不以子代理自报为准。通过则 `accept`，附主控自己的 `checks`、证据文件和空 `unresolved`；不通过则 `fail`，release 后视情况 `retry`。
8. **收尾**：全部任务 accepted 后 `finish`；无法完成时 `close-run`（保留全部失败记录）。最终报告区分「已验证」「未验证」「已降级」三类，可在 `finish` 时以 `report` 固定其哈希。项目有 checkpoint 流程时照做。

## 4. 并发与写入所有权

- 并发上限取项目 `.codex/config.toml`、再取 `$CODEX_HOME/config.toml` 中的 `[agents].max_concurrent_threads_per_session`，都没有时为 6；项目配置 `max_live` 只能把它调低。同一项目所有进行中的 run 共享名额和写入占用。
- 写入范围按真实路径和目录包含关系判断冲突，同一时刻一个路径只属于一个任务。一个任务拥有其写入范围内的全部修改，包括测试与生成文件。
- 子代理不得写入状态目录、`.git`、`.codex`、`.agents`、`.env` 和配置的 `protected_paths`；写入范围不能是项目根。
- reviewer、researcher 是只读岗，`write_scope` 必须为空，交付的 `artifacts` 必须为空。
- 只有主控派发；子代理不再派生代理。不得留下脱离控制的后台进程。

## 5. 审查发现、返工与恢复

- reviewer 独立阅读要求和产物，问题写入 `findings`（位置、触发条件、影响、证据、是否阻断）。
- 主控 accept 审查任务时逐条处置 findings：`confirmed` 或 `rejected`，都要附证据。已确认的阻断项必须指向同一 run 中已登记的修复任务 `followup_task_id`（先 `add-task` 修复任务，再 accept 审查）。
- 修复由 engineer（或原岗位）完成，修复任务 accepted 后再登记复审任务；`finish` 要求全部任务 accepted，因此修复和复审都必须完成。
- 每个任务最多两次尝试（`max_attempts` 1 或 2）。失败如实保留：不靠新任务、新 run 或新会话掩盖失败；需要改合同时另立带 `revises` 的修订任务。
- 中断恢复：`status` → 核对原生 Agent 实际状态 → `interrupt` → 确认终止 → `release` → `retry`。上下文压缩后，先重读本文件、合同和 `status` 再继续。

## 6. 项目专用岗位

- 需要为项目固定模型、推理档位或补充规则时，只修改项目 `.codex/orchestration.json`，再运行 `roles.py generate --root .`；不要手改生成的岗位文件。
- 生成的岗位需新开会话才会加载；之后重新抄录已加载岗位并 `set-loaded-agents`。
- 写入项目 `.codex/` 和全局 `$CODEX_HOME/agents/`（`roles.py install-global`）都属于配置变更，先请求用户批准。
- 详细规则见 [项目岗位](references/project-roles.md)。

## 7. 如实性约束

- 不冒充具名派发：未加载岗位时只能 inline 或停止，并如实记录 `degraded_reason`。
- 不声称模型身份或推理档位已生效；岗位文件和目录声明只是期望配置。
- 不采集、不报告 token 用量或费用。
- 岗位文件里的 `sandbox_mode` 和合同范围不是操作系统隔离。实测（Codex CLI 0.167.2，主控以 `-s workspace-write` 运行）中，带 `sandbox_mode = "read-only"` 的 reviewer 子代理实际沙箱仍是 workspace-write，只读约束只靠岗位指令；主控须审查实际命令、输出和文件变化（如 `git status`）。

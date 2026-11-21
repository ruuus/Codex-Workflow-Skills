# 合同与交付

本文件说明 `journal.py` 的合同、交付对象、派发消息和恢复流程。路径可写成相对项目根的形式，也可写绝对路径。

## 合同（add-task 输入）

```json
{
  "run_id": "r20260101-a",
  "task_id": "fix-parser",
  "role": "engineer",
  "objective": "修复 parser.parse_line 遇到空行时抛出 IndexError 的问题",
  "inputs": [
    {"path": "docs/spec.md", "sha256": "<64 位小写十六进制>"},
    {"path": ".orchestration/preflight.txt", "sha256": "<64 位小写十六进制>"}
  ],
  "read_scope": ["docs/spec.md", "src/parser.py", "tests/test_parser.py"],
  "write_scope": ["src/parser.py", "tests/test_parser.py"],
  "dependencies": ["review-parser"],
  "acceptance": ["空行返回 None", "新增的回归测试先失败后通过", "原有测试全部通过"],
  "failure_conditions": ["读取或写入超出合同范围", "执行 allowed_calls 之外的命令"],
  "allowed_calls": ["python -m unittest tests.test_parser"],
  "execution_instructions": "先运行测试复现，再修改。",
  "max_attempts": 2
}
```

- 必填：`run_id`、`task_id`、`role`、`objective`、`inputs`、`read_scope`、`write_scope`、`dependencies`、`acceptance`（非空）、`failure_conditions`。
- 可选：`allowed_calls`、`execution_instructions`、`max_attempts`（1 或 2，默认 2）、`requested_agent`（只能在岗位的启用候选中选）、`revises{run_id, task_id, attempt, reason}`。
- `inputs` 可以指向项目外的绝对路径（例如技能文件），账本只校验哈希。登记和 start 时都会复核。
- 只读岗（reviewer、researcher）的 `write_scope` 必须为空。
- 账本补充的字段：`contract_schema`、`read_scope_added_from_write`（写入范围自动并入读取范围）、`baseline_calls`、`project_rules`、`dispatch`；inline 时还有 `role_instructions`。start 导出时加上 `attempt`。

## baseline_calls 与 allowed_calls

- `baseline_calls` 由账本写入，始终可用：读取合同和范围内文件并计算哈希、整理输出格式、非递归列出范围内明确列出的目录、在写入范围内写入和回读、为看清退出码重跑同一条只读命令。子代理换一种只读方式不算越界。
- `allowed_calls` 只列基线之外的执行：运行测试、执行脚本、启动进程、联网、导入或调用业务函数。需要清单外的执行时，子代理停止该步骤并写入 `unresolved`，由主控修订合同。

## 静态阅读与执行验证

- 在 `execution_instructions` 与 `acceptance` 中写清任务是静态阅读还是执行验证。
- 静态任务可以读文件、计算哈希、摘录内容，但不运行受审代码、测试或业务命令。
- 静态审查发现需要运行才能确认的疑点时，返回位置、触发条件和建议的验证方法，由主控另立执行合同，并在其中列明解释器、命令、样本和可写目录。
- 项目整体的 Git 状态、临时文件扫描和保护路径复核由主控统一完成，子代理不为证明“没写别处”而递归枚举项目。

## 派发消息

start 返回的 `message` 就是派发消息，只包含：

```text
岗位：engineer（orch_engineer）
run=r20260101-a task=fix-parser attempt=1
权威合同：/abs/path/.orchestration/contracts/r20260101-a/fix-parser-attempt-1.json
合同 SHA-256：<64 位十六进制>
先读取该合同，核对文件 SHA-256 与全部 inputs 哈希，不符即停止并报告。
按合同执行，最终回复只包含一个 JSON 交付对象。
```

inline 派发时多一行：岗位职责在合同 `role_instructions` 字段中，必须完整遵守。不要在消息里转抄合同正文。

## 交付对象

```json
{
  "contract_receipt": {"path": "<合同绝对路径>", "sha256": "<合同哈希>", "inputs_verified": true},
  "operations": [{"action": "python -m unittest tests.test_parser", "exit_code": 0, "result": "Ran 5 tests, OK"}],
  "artifacts": [{"path": "src/parser.py", "sha256": "<修改后哈希>"}],
  "checks": [{"name": "空行返回 None", "passed": true, "evidence": "test_blank_line 通过"}],
  "unresolved": [],
  "observations": ["src/io.py 也有类似写法，建议另立任务"]
}
```

reviewer 另含 `findings`（无问题时为空数组）：

```json
"findings": [
  {"location": "src/parser.py:42", "trigger": "输入为空行", "impact": "抛出 IndexError 导致整批导入中止",
   "evidence": "line.split()[0] 在空列表上取下标", "blocking": true}
]
```

`check-report` 与 `report` 的检查：

- `operations`、`checks` 非空，字段类型正确；`exit_code` 为整数，确实没有退出码的操作写 `null`。
- 有 `passed=false` 时 `unresolved` 必须非空；`unresolved` 非空却全部通过视为矛盾。
- 只有 reviewer 可以有非空 `findings`；只读岗的 `artifacts` 必须为空。
- 回执必须与本次导出的合同路径和哈希一致，且 `inputs_verified=true`。
- `report` 另外要求产物都在写入范围内，且哈希与磁盘一致。
- 不一致时账本保存原文并要求同一代理更正一次（不计新尝试）；第二次仍不一致记为 failed。

`unresolved` 只放阻断本任务验收的问题；合同之外的事实和建议放 `observations`，由主控决定是否另立任务。

## 验收（accept 输入）

```json
{
  "run_id": "r20260101-a",
  "task_id": "review-parser",
  "decision": {
    "checks": [{"name": "主控复现 finding #0", "passed": true, "evidence": "空行输入抛出 IndexError"}],
    "evidence": [".orchestration/runs/r20260101-a/review-check.txt"],
    "unresolved": [],
    "findings": [
      {"index": 0, "disposition": "confirmed", "evidence": "主控复现成功", "followup_task_id": "fix-parser"}
    ]
  }
}
```

- 交付中的检查必须全部通过、`unresolved` 为空；主控的 `checks` 必须全部通过，`evidence` 至少一个存在的文件，账本固定其哈希。
- 产物在交付后被改动会被拒绝。
- reviewer 任务必须逐条处置 findings；已确认的阻断项必须指向同一 run 中已登记的修复任务。

## 原生工具对照

| 动作 | v1 | v2 |
|---|---|---|
| 派发 | `spawn_agent`：`agent_type`（named）、`fork_context=false`、`message` | `spawn_agent`：`agent_type`（named）、`task_name`、`fork_turns="none"`、`message` |
| 句柄 | 返回的 Agent id | `task_name` 对应的任务路径 |
| 等待 | `wait_agent` | 会话提供的等待方式（完成消息会以 `FINAL_ANSWER` 送达） |
| 补充消息 | `send_input` | `followup_task`（触发新一轮）或 `send_message` |
| 关闭 | `close_agent` | 会话提供的关闭工具 |

以当前会话实际提供的工具与参数为准；bind 时如实填写 `tool_version` 和实际参数。不传 `model` 和 `reasoning_effort`。

## 中断与恢复

1. `status --run-id <run>` 查看哪些尝试仍占用（live）。
2. 核对原生 Agent 的实际状态，不凭记忆推断。
3. 仍在运行但需要停止：关闭它，`interrupt` 记录原因。
4. 确认终止后 `release`，未达上限时 `retry`，重新 start、派发、bind。
5. 上下文压缩或会话重启后，先重读 SKILL.md、相关合同和 `status`，再继续；不要靠记忆重建状态。

## 修订合同与结束

- 已开始的合同不可修改。范围或要求需要调整时，另立任务并写 `revises{run_id, task_id, attempt, reason}`；旧尝试、旧产物原样保留。
- 从未开始过的任务可以 `rebind`，按当前配置和已加载岗位重新选择。
- 无法完成的 run 用 `close-run`（`reason`、`evidence`），状态记为 `closed_with_failures`；要求没有占用中、执行中或待验收的任务。复验另建新 run。

# Codex-Workflow-Skills

> Reusable workflow skills for the Codex CLI. The first skill, `multi-agent-orchestration`, lets the main Codex session split work into role-based subagent tasks (worker, engineer, reviewer, researcher, editor), hand each one a hashed contract file, track write ownership in a SQLite ledger, and accept results only after independent verification. Pure Python standard library, MIT licensed.

Codex 工作流技能仓库：收录可在任意项目中复用的 Codex 技能。

## 简介与特性

首个技能 **multi-agent-orchestration**（多代理编排）：

- **岗位派发**：五个逻辑岗位 worker、engineer、reviewer、researcher、editor，职责只在 `assets/roles.toml` 中定义一次。
- **合同交接**：合同先写入账本再派发；派发消息只传合同文件的绝对路径和 SHA-256，子代理自行核对。
- **写入所有权**：写入范围独占，按真实路径和目录包含关系判断冲突；同一项目的所有 run 共享并发名额。
- **独立验收**：前置任务被主控 accept 后才算完成；审查独立进行，阻断问题必须登记修复任务并复审。
- **如实记录**：每个任务最多两次尝试，失败原样保留；未加载具名岗位时如实降级为 inline 并告知用户。
- **项目岗位（可选）**：按项目配置生成专用岗位，固定模型与推理档位（支持 `max-available`）。

## 安装

技能安装在 `$CODEX_HOME/skills/`（默认 `~/.codex/skills/`）。以下两种方式只选其一。

**方式一：skill-installer（Codex 自带）**

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" `
  --repo ruuus/Codex-Workflow-Skills --path skills/multi-agent-orchestration
```

**方式二：本仓库的安装脚本（支持更新）**

```powershell
git clone https://github.com/ruuus/Codex-Workflow-Skills.git
cd Codex-Workflow-Skills
python -B tools/install_skill.py multi-agent-orchestration
```

脚本写入 `.skill-install.json` 清单，再次运行时按清单更新。目标目录被改动或含清单外文件时拒绝，加 `--replace` 会先备份到 `$CODEX_HOME/backups/<技能>/<时间戳>/` 再替换。

方式一的目标已存在时会报错，更新前需先移走旧目录。Linux / macOS 把 `$env:USERPROFILE\...` 换成 `~/...` 即可。

## 安装全局岗位

```powershell
python -B -X utf8 "$env:USERPROFILE\.codex\skills\multi-agent-orchestration\scripts\roles.py" install-global
```

生成 `$CODEX_HOME/agents/orch_worker.toml` 等五个岗位文件。它们不写模型和推理档位，继承全局设置；reviewer、researcher 另写 `sandbox_mode = "read-only"`。**装完需要新开 Codex 会话才会加载。** 不安装也能用：技能会降级为 inline 派发并说明原因。

用 `roles.py doctor` 查看岗位状态（missing / current / outdated / modified / foreign）。

## 快速开始

在任意项目中对 Codex 说：

```text
使用 $multi-agent-orchestration 按岗位拆分任务、派发子代理，并由主控独立验收。
```

主控会读取 `SKILL.md`，用 `journal.py` 建立 run、登记合同、派发、回收与验收。账本保存在项目的 `.orchestration/`（自带 `.gitignore`）。

**可选的项目配置**：在项目中创建 `.codex/orchestration.json`（示例见 `skills/multi-agent-orchestration/assets/orchestration.example.json`），可设置保护路径、启动必读文件、preflight、项目规则、并发上限和项目专用岗位。配置岗位后运行：

```powershell
python -B -X utf8 <技能目录>\scripts\roles.py generate --root .
```

详见 `references/project-roles.md`。

## 目录结构

```text
skills/multi-agent-orchestration/
  SKILL.md                     技能正文（主控流程）
  agents/openai.yaml           界面元数据
  assets/roles.toml            岗位职责（唯一来源）
  assets/orchestration.example.json
  references/contracts.md      合同、交付、派发消息与恢复
  references/project-roles.md  项目岗位、模型目录与推理档位
  scripts/orchlib.py           共享库
  scripts/journal.py           账本
  scripts/roles.py             岗位安装、生成与诊断
tests/                         单元测试
tools/install_skill.py         安装脚本
```

## 开发测试

```powershell
python -B -X utf8 -m unittest discover -s tests -v
```

测试全部使用临时目录和临时 `CODEX_HOME`，不会改动真实的全局目录。

## 兼容性

- Codex CLI 0.167.2 起；Python ≥ 3.11（只用标准库）。
- Windows 实测；Linux 由 CI 覆盖；macOS 未测试。

## 贡献者

- **ruuus**：维护者，负责需求、设计决策与验收。
- **Codex**：AI 协作开发者，负责实现、测试与文档；提交中以 `Co-authored-by` 标注。

## 许可证

[MIT](LICENSE)

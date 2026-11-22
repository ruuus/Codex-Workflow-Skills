---
name: multi-agent-orchestration
description: Coordinator-led multi-agent orchestration for Codex. Use only when the user explicitly asks for multi-agent or subagent work division, parallel implementation, or independent review. Dispatches Codex subagents by role (worker, engineer, reviewer, researcher, editor) and records contracts, file ownership, and acceptance in a SQLite ledger; can generate project-specific roles and model candidates. Works with any project; if the project ships its own orchestration skill, that skill takes precedence.
license: MIT
---

# Multi-Agent Orchestration

The coordinator (the current session) splits work, writes contracts, dispatches subagents, and accepts results independently; subagents only execute their contracts. The ledger `scripts/journal.py` records every step, and `scripts/roles.py` manages role definitions. The scripts use only the Python 3.11+ standard library. Below, `<skill>` is this skill's directory; run commands from the project root:

```powershell
python -B -X utf8 <skill>/scripts/journal.py --root . <command> --input <JSON file>
python -B -X utf8 <skill>/scripts/roles.py doctor --root .
```

Pass JSON input as a UTF-8 file or via stdin; do not splice long text into the command line. Full contract and delivery formats: [Contracts and deliveries](references/contracts.md). Project-specific roles: [Project roles](references/project-roles.md).

## 1. When to use

- Use only when the user explicitly asks for multi-agent or subagent work division, parallel implementation, or independent review.
- Do small tasks the coordinator can finish directly; do not split work for form's sake.
- If the project has its own orchestration skill (for example, `.codex/orchestration.json` lacks this skill's `schema`), the project wins; this skill's scripts reject that config.

## 2. Startup

1. Read the project's `AGENTS.md` and the files listed in the project config's `startup.read`.
2. Run `journal.py config` and `roles.py doctor --root .` to confirm the state directory, concurrency limit, protected paths, role status, and model catalog.
3. If `startup.preflight` is configured, the coordinator runs that command once per session turn, writes the output to the `result` path, and references it as a contract input; subagents do not rerun it.
4. Record loaded roles: copy the exact role names offered in the `agent_type` parameter description of the session's native dispatch tool (`spawn_agent`); use an empty array if the parameter does not exist. Do not substitute a listing of role files on disk. Copy every name in the description, not just those relevant to this run, and check the list against the description before submitting; a missed name downgrades tasks that could have been dispatched by name.
5. `create-run`: `{"run_id", "objective", "loaded_agents": {"observed_from": "where the names were copied from", "names": [...]}}`. After the session changes (new session, roles reloaded), update with `set-loaded-agents`.

## 3. Main loop

1. **Register**: `add-task` records contracts (one, or a `{"tasks":[...]}` batch applied atomically). The ledger selects the role and adds `dispatch`, `project_rules`, and `baseline_calls`; on inline dispatch it also adds `role_instructions`. If a `notice` is returned, tell the user the fallback reason.
2. **Start**: `start` checks that dependencies are accepted, a concurrency slot is free, write scopes do not overlap, inputs hashes match, and role selection has not drifted; it exports an immutable contract file and returns its absolute path, SHA-256, and the dispatch `message`.
3. **Dispatch**: dispatch with the native tool; the message is exactly `message` (role, run/task/attempt, contract path and hash, verification instruction). Do not paste the contract body.
   - named: pass `agent_type=dispatch.agent_type`; inline: omit `agent_type` entirely (no placeholder such as `default`), and record in `bind` exactly what was passed.
   - No inherited context: v1 `fork_context=false`, v2 `fork_turns="none"`.
   - Do not pass `model` or `reasoning_effort`.
   - If dispatch is refused and no agent handle was obtained, use `cancel-start` (does not count as an attempt).
4. **Bind**: `bind` records the agent handle and actual spawn arguments `spawn{tool_version, agent_type, inherits_context, model_override, reasoning_effort_override}`; each attempt can be bound only once.
5. **Collect**: wait with the tool the session actually provides, then hand the raw reply to the ledger: `check-report` first, then `report` (`reply` or `reply_path`). The ledger keeps every raw version. If the delivery is inconsistent, ask the same agent once for a correction via a follow-up message (not a new attempt); a second inconsistent delivery is recorded as failed automatically.
6. **Release**: close the agent and confirm it has terminated (based on the close tool's result or status), then `release` (`terminal_confirmed:true` plus the basis). Until release, the write scope and concurrency slot stay occupied.
7. **Accept**: the coordinator independently verifies the artifacts (read files, run tests, inspect diffs) rather than trusting the subagent's self-report. On success, `accept` with the coordinator's own `checks`, evidence files, and empty `unresolved`; otherwise `fail`, then after release `retry` if appropriate.
8. **Finish**: once all tasks are accepted, `finish`; if the run cannot be completed, `close-run` (all failure records are kept). The final report separates "verified", "unverified", and "degraded"; its hash can be pinned via `report` at `finish`. Follow the project's checkpoint process if it has one.

## 4. Concurrency and write ownership

- The concurrency limit is `[agents].max_concurrent_threads_per_session` from the project `.codex/config.toml`, then `$CODEX_HOME/config.toml`, defaulting to 6; the project config's `max_live` can only lower it. All active runs in a project share slots and write ownership.
- Write-scope conflicts are decided by real paths and directory containment; at any moment a path belongs to one task. A task owns every change within its write scope, including tests and generated files.
- Subagents must not write to the state directory, `.git`, `.codex`, `.agents`, `.env`, or configured `protected_paths`; a write scope cannot be the project root.
- reviewer and researcher are read-only roles: `write_scope` must be empty, and delivered `artifacts` must be empty.
- Only the coordinator dispatches; subagents do not spawn further agents. Leave no uncontrolled background processes.

## 5. Findings, rework, and recovery

- The reviewer independently reads requirements and artifacts and records issues in `findings` (location, trigger, impact, evidence, blocking).
- When accepting a review task, the coordinator dispositions every finding as `confirmed` or `rejected`, each with evidence. A confirmed blocking finding must point to a fix task `followup_task_id` already registered in the same run (`add-task` the fix first, then accept the review).
- The engineer (or the original role) does the fix; after the fix task is accepted, register a re-review task. `finish` requires every task to be accepted, so both fix and re-review must complete.
- Each task gets at most two attempts (`max_attempts` 1 or 2). Keep failures as they are: do not hide them behind new tasks, new runs, or new sessions; when a contract must change, open a revision task with `revises`.
- Interrupt recovery: `status` → check the native agent's actual state → `interrupt` → confirm termination → `release` → `retry`. After context compaction, reread this file, the contracts, and `status` before continuing.

## 6. Project-specific roles

Role cards have three layers that change independently: role duties (this skill's `assets/roles.toml`), project emphasis (`rules` and per-role `role_focus` in the project config), and model assignment (candidates with model, effort, and priority).

- To start a project config from the user's default, run `roles.py init --root .` (preset `$CODEX_HOME/orchestration/presets/default.json`, or `--preset <name>`); then adjust `rules` and `role_focus` for the project.
- To pin models, reasoning effort, or extra rules for a project, edit only the project's `.codex/orchestration.json` and run `roles.py generate --root .`; do not hand-edit generated role files.
- `role_focus` reaches every dispatch mode: it is written into each contract, and also into generated project role files.
- `roles.py doctor --root .` reports `advisories` from the user's model profile (`$CODEX_HOME/orchestration/models.json`): retired or unprofiled models, role/model mismatches, and a reviewer whose first-choice model shares a family with the worker or engineer. Advisories never change selection; relay them to the user rather than editing the config unasked.
- When the user's preset changes, `roles.py diff-preset --root .` shows what differs; sync only with the user's approval.
- Generated roles load only in a new session; afterwards, recopy the loaded roles and run `set-loaded-agents`.
- Codex loads project-local configuration, including `.codex/agents/`, only for trusted projects. If no `proj_*` roles appear in the spawn tool's `agent_type` list, ask the user to trust the project (in the app, or `codex exec --trust` for non-interactive runs) rather than falling back.
- Writing the project's `.codex/` or the global `$CODEX_HOME/agents/` (`roles.py install-global`) is a configuration change; ask the user for approval first.
- Details: [Project roles](references/project-roles.md).

## 7. Honesty constraints

- Never pretend to dispatch by name: when a role is not loaded, either dispatch inline or stop, and record `degraded_reason` truthfully.
- Do not claim a model identity or reasoning effort took effect; role files and catalog entries only declare the intended configuration.
- Do not collect or report token usage or cost.
- `sandbox_mode` in role files and contract scopes are not OS-level isolation. In testing (Codex CLI 0.167.2, coordinator run with `-s workspace-write`), a reviewer subagent with `sandbox_mode = "read-only"` still ran with a workspace-write sandbox; the read-only constraint relies on role instructions alone, so the coordinator must review actual commands, output, and file changes (for example, `git status`).

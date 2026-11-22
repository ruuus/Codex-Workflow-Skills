# Contracts and Deliveries

This file describes `journal.py` contracts, delivery objects, dispatch messages, and recovery. Paths may be relative to the project root or absolute.

## Contract (add-task input)

```json
{
  "run_id": "r20260101-a",
  "task_id": "fix-parser",
  "role": "engineer",
  "objective": "Fix parser.parse_line raising IndexError on blank lines",
  "inputs": [
    {"path": "docs/spec.md", "sha256": "<64 lowercase hex chars>"},
    {"path": ".orchestration/preflight.txt", "sha256": "<64 lowercase hex chars>"}
  ],
  "read_scope": ["docs/spec.md", "src/parser.py", "tests/test_parser.py"],
  "write_scope": ["src/parser.py", "tests/test_parser.py"],
  "dependencies": ["review-parser"],
  "acceptance": ["Blank lines return None", "The new regression test fails before the fix and passes after", "All existing tests pass"],
  "failure_conditions": ["Reading or writing outside the contract scope", "Running commands outside allowed_calls"],
  "allowed_calls": ["python -m unittest tests.test_parser"],
  "execution_instructions": "Reproduce with the tests first, then change the code.",
  "max_attempts": 2
}
```

- Required: `run_id`, `task_id`, `role`, `objective`, `inputs`, `read_scope`, `write_scope`, `dependencies`, `acceptance` (non-empty), `failure_conditions`.
- Optional: `allowed_calls`, `execution_instructions`, `max_attempts` (1 or 2, default 2), `requested_agent` (only among the role's enabled candidates), `revises{run_id, task_id, attempt, reason}`.
- `inputs` may point to absolute paths outside the project (for example, skill files); the ledger only checks their hashes, at registration and again at start.
- Read-only roles (reviewer, researcher) must have an empty `write_scope`.
- Fields added by the ledger: `contract_schema`, `read_scope_added_from_write` (write scope merged into read scope), `baseline_calls`, `project_rules`, `dispatch`; on inline dispatch also `role_instructions`. `start` adds `attempt` when exporting.

## baseline_calls and allowed_calls

- `baseline_calls` is written by the ledger and always allowed: reading the contract and in-scope files and hashing them, reshaping output, non-recursively listing directories explicitly named in scope, writing and reading back within the write scope, and rerunning the same read-only command to see its exit code. Using a different read-only method is not out of scope.
- `allowed_calls` lists only execution beyond the baseline: running tests, executing scripts, starting processes, network access, importing or calling business functions. When execution outside the list is needed, the subagent stops that step and records it in `unresolved` for the coordinator to revise the contract.

## Static reading vs. execution

- State in `execution_instructions` and `acceptance` whether the task is static reading or execution-based verification.
- Static tasks may read files, compute hashes, and quote content, but do not run the reviewed code, tests, or business commands.
- When a static review finds something that can only be confirmed by running it, return the location, trigger, and suggested verification; the coordinator opens an execution contract that names the interpreter, commands, samples, and writable directories.
- Project-wide Git status, temp-file scans, and protected-path checks are done by the coordinator; subagents do not recursively enumerate the project to prove they wrote nothing elsewhere.

## Dispatch message

The `message` returned by `start` is the dispatch message, and contains only:

```text
Role: engineer (orch_engineer)
run=r20260101-a task=fix-parser attempt=1
Authoritative contract: /abs/path/.orchestration/contracts/r20260101-a/fix-parser-attempt-1.json
Contract SHA-256: <64 hex chars>
Read this contract first and verify its SHA-256 and every inputs hash; on any mismatch, stop and report.
Execute the contract; your final reply must contain exactly one JSON delivery object.
```

On inline dispatch there is one extra line saying the role instructions are in the contract's `role_instructions` field and must be followed in full. Do not paste the contract body into the message.

## Delivery object

```json
{
  "contract_receipt": {"path": "<absolute contract path>", "sha256": "<contract hash>", "inputs_verified": true},
  "operations": [{"action": "python -m unittest tests.test_parser", "exit_code": 0, "result": "Ran 5 tests, OK"}],
  "artifacts": [{"path": "src/parser.py", "sha256": "<hash after change>"}],
  "checks": [{"name": "Blank lines return None", "passed": true, "evidence": "test_blank_line passes"}],
  "unresolved": [],
  "observations": ["src/io.py has a similar pattern; consider a separate task"]
}
```

The reviewer also includes `findings` (an empty array when there are no issues):

```json
"findings": [
  {"location": "src/parser.py:42", "trigger": "Input is a blank line", "impact": "IndexError aborts the whole import batch",
   "evidence": "line.split()[0] indexes an empty list", "blocking": true}
]
```

Checks performed by `check-report` and `report`:

- `operations` and `checks` are non-empty with correct field types; `exit_code` is an integer, or `null` for operations that genuinely have none.
- If any check has `passed=false`, `unresolved` must be non-empty; non-empty `unresolved` with all checks passing is a contradiction.
- Only the reviewer may have non-empty `findings`; read-only roles must have empty `artifacts`.
- The receipt must match this attempt's exported contract path and hash, with `inputs_verified=true`.
- `report` additionally requires artifacts to be within the write scope with hashes matching disk.
- On inconsistency the ledger keeps the raw reply and asks the same agent for one correction (not a new attempt); a second inconsistent reply is recorded as failed.

`unresolved` holds only issues that block this task's acceptance; facts and suggestions outside the contract go in `observations`, and the coordinator decides whether to open new tasks.

## Acceptance (accept input)

```json
{
  "run_id": "r20260101-a",
  "task_id": "review-parser",
  "decision": {
    "checks": [{"name": "Coordinator reproduced finding #0", "passed": true, "evidence": "Blank-line input raises IndexError"}],
    "evidence": [".orchestration/runs/r20260101-a/review-check.txt"],
    "unresolved": [],
    "findings": [
      {"index": 0, "disposition": "confirmed", "evidence": "Reproduced by the coordinator", "followup_task_id": "fix-parser"}
    ]
  }
}
```

- Every check in the delivery must pass and `unresolved` must be empty; the coordinator's `checks` must all pass, and `evidence` must include at least one existing file, whose hash the ledger pins.
- Artifacts changed after delivery are rejected.
- Review tasks must disposition every finding; a confirmed blocking finding must point to a fix task registered in the same run.

## Native tool mapping

| Action | v1 | v2 |
|---|---|---|
| Dispatch | `spawn_agent`: `agent_type` (named), `fork_context=false`, `message` | `spawn_agent`: `agent_type` (named), `task_name`, `fork_turns="none"`, `message` |
| Handle | returned agent id | task path corresponding to `task_name` |
| Wait | `wait_agent` | the session's wait mechanism (completion arrives as `FINAL_ANSWER`) |
| Follow-up | `send_input` | `followup_task` (starts a new turn) or `send_message` |
| Close | `close_agent` | the session's close tool |

Use the tools and parameters the current session actually provides; fill in `tool_version` and the actual arguments truthfully at bind. Do not pass `model` or `reasoning_effort`.

## Interruption and recovery

1. `status --run-id <run>` shows which attempts are still live.
2. Check the native agent's actual state; do not infer from memory.
3. If it is still running but must stop: close it and record the reason with `interrupt`.
4. After confirming termination, `release`; if under the attempt limit, `retry`, then start, dispatch, and bind again.
5. After context compaction or a session restart, reread SKILL.md, the relevant contracts, and `status` before continuing; do not rebuild state from memory.

## Revising contracts and ending runs

- A started contract cannot be modified. When scope or requirements change, open a new task with `revises{run_id, task_id, attempt, reason}`; old attempts and artifacts stay as they are.
- A task that has never started can be `rebind`-ed to reselect its role from the current config and loaded roles.
- A run that cannot be completed is closed with `close-run` (`reason`, `evidence`) and recorded as `closed_with_failures`; no task may be live, running, or awaiting acceptance. Re-verification uses a new run.
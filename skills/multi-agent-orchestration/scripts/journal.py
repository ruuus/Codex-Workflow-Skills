"""多代理编排账本：在 SQLite 中记录 run、任务合同、尝试、文件写入占用与主控验收。

用法：python journal.py --root <项目> <命令> [--input 文件]（JSON 也可从 stdin 传入）
命令：config status create-run set-loaded-agents add-task rebind start cancel-start bind
      check-report report release accept fail interrupt retry finish close-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orchlib as ol  # noqa: E402
from orchlib import OrchError  # noqa: E402

APPLICATION_ID = 0x4D414F31
USER_VERSION = 1
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_FIELDS = (
    "run_id", "task_id", "role", "objective", "inputs", "read_scope", "write_scope",
    "dependencies", "acceptance", "failure_conditions",
)
OPTIONAL_FIELDS = ("allowed_calls", "execution_instructions", "max_attempts", "requested_agent", "revises")
DELIVERY_KEYS = {"contract_receipt", "operations", "artifacts", "checks", "unresolved", "observations", "findings"}
BASELINE_CALLS = [
    "Read the authoritative contract and files within read_scope and write_scope by any read-only means, and compute SHA-256",
    "Organize, filter, or transform command output (e.g. parse JSON, select fields, format)",
    "List, non-recursively, directories explicitly named in read_scope or write_scope",
    "Create, modify, and read back files within write_scope, and compute SHA-256",
    "Rerun the same read-only command to obtain its exit code or full output",
]

SCHEMA_SQL = """
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  objective TEXT NOT NULL,
  status TEXT NOT NULL,
  loaded_json TEXT NOT NULL,
  close_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE tasks (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,
  contract_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id)
);
CREATE TABLE attempts (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  live INTEGER NOT NULL,
  contract_path TEXT NOT NULL,
  contract_sha256 TEXT NOT NULL,
  agent_handle TEXT,
  spawn_json TEXT,
  delivery_json TEXT,
  corrections INTEGER NOT NULL DEFAULT 0,
  release_json TEXT,
  decision_json TEXT,
  reason TEXT,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id, attempt)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  task_id TEXT,
  attempt INTEGER,
  kind TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  at TEXT NOT NULL
);
"""


def _check_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ol.ID_RE.match(value):
        raise OrchError(f"{label} 须为 1–64 位字母、数字、点、下划线或连字符，且以字母或数字开头")
    return value


def _require_keys(payload: Any, required: set[str], optional: set[str] = frozenset(), label: str = "输入") -> dict:
    if not isinstance(payload, dict):
        raise OrchError(f"{label} 必须是 JSON 对象")
    missing = required - set(payload)
    if missing:
        raise OrchError(f"{label} 缺少字段：{', '.join(sorted(missing))}")
    extra = set(payload) - required - set(optional)
    if extra:
        raise OrchError(f"{label} 含未知字段：{', '.join(sorted(extra))}")
    return payload


def _nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchError(f"{label} 必须是非空字符串")
    return value


def _str_list(value: Any, label: str, nonempty: bool = False) -> list[str]:
    if not ol.is_str_list(value):
        raise OrchError(f"{label} 必须是非空字符串组成的数组")
    if nonempty and not value:
        raise OrchError(f"{label} 不能为空")
    return list(value)


class Journal:
    def __init__(self, root: Path):
        self.root = Path(os.path.realpath(root))
        if not self.root.is_dir():
            raise OrchError(f"项目根不存在：{root}")
        self.cfg = ol.load_project_config(self.root)
        self.state_dir = ol.resolve_in_root(self.root, self.cfg["state_dir"])
        self.db_path = self.state_dir / "state.sqlite3"
        self.conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ 基础设施

    def protected(self) -> list[str]:
        items = [self.state_dir] + [self.root / name for name in ol.DEFAULT_PROTECTED]
        items += [ol.resolve_in_root(self.root, item) for item in self.cfg["protected_paths"]]
        return [ol.norm_real(item) for item in items]

    def limit(self) -> dict[str, Any]:
        native = ol.native_thread_limit(self.root)
        value = native["value"]
        if self.cfg["max_live"] is not None:
            value = min(value, self.cfg["max_live"])
        return {"native": native, "max_live": self.cfg["max_live"], "effective": value}

    def open(self, create: bool = False) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn
        exists = self.db_path.exists()
        if not exists:
            if not create:
                raise OrchError(f"账本不存在：{self.db_path}；先用 create-run 建立")
            for leftover in ("contracts", "runs"):
                folder = self.state_dir / leftover
                if folder.is_dir() and any(folder.iterdir()):
                    raise OrchError(f"状态目录已有 {leftover} 记录但账本缺失，拒绝隐式重建：{self.state_dir}")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            gitignore = self.state_dir / ".gitignore"
            if not gitignore.exists():
                ol.write_text_lf(gitignore, "*\n")
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise OrchError(f"{self.db_path} 不是可用的 SQLite 账本：{exc}") from exc
        if not exists:
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version={USER_VERSION}")
            conn.execute("COMMIT")
        elif app_id != APPLICATION_ID:
            conn.close()
            raise OrchError(f"{self.db_path} 不是本技能的账本（application_id 不符），拒绝使用")
        elif version != USER_VERSION:
            conn.close()
            raise OrchError(f"账本版本 {version} 不受支持（需要 {USER_VERSION}），拒绝使用")
        conn.execute("PRAGMA foreign_keys=ON")
        self.conn = conn
        return conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def tx(self):
        conn = self.open()

        class _Tx:
            def __enter__(self):
                conn.execute("BEGIN IMMEDIATE")
                return conn

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    conn.execute("COMMIT")
                else:
                    conn.execute("ROLLBACK")
                return False

        return _Tx()

    def event(self, conn, run_id, task_id, attempt, kind, detail) -> None:
        conn.execute(
            "INSERT INTO events(run_id, task_id, attempt, kind, detail_json, at) VALUES (?,?,?,?,?,?)",
            (run_id, task_id, attempt, kind, ol.canonical_json(detail), ol.now_local()),
        )

    def run_dir(self, run_id: str) -> Path:
        return self.state_dir / "runs" / run_id

    def get_run(self, conn, run_id: str, need_open: bool = True) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise OrchError(f"run 不存在：{run_id}")
        if need_open and row["status"] != "open":
            raise OrchError(f"run {run_id} 已结束（{row['status']}）")
        return row

    def get_task(self, conn, run_id: str, task_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        if row is None:
            raise OrchError(f"任务不存在：{run_id}/{task_id}")
        return row

    def last_attempt(self, conn, run_id: str, task_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM attempts WHERE run_id=? AND task_id=? ORDER BY attempt DESC LIMIT 1", (run_id, task_id)
        ).fetchone()

    def counted_attempts(self, conn, run_id: str, task_id: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE run_id=? AND task_id=? AND status!='cancelled'", (run_id, task_id)
        ).fetchone()[0]

    def set_task(self, conn, run_id, task_id, status) -> None:
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE run_id=? AND task_id=?",
            (status, ol.now_local(), run_id, task_id),
        )

    def set_attempt(self, conn, row, **fields) -> None:
        fields["updated_at"] = ol.now_local()
        names = ", ".join(f"{key}=?" for key in fields)
        conn.execute(
            f"UPDATE attempts SET {names} WHERE run_id=? AND task_id=? AND attempt=?",
            (*fields.values(), row["run_id"], row["task_id"], row["attempt"]),
        )

    def loaded_names(self, run: sqlite3.Row) -> list[str]:
        return json.loads(run["loaded_json"])["names"]

    # ------------------------------------------------------------ 路径

    def abs_path(self, value: str) -> Path:
        return ol.resolve_in_root(self.root, value)

    def inside(self, path: Path) -> bool:
        return ol.path_within(ol.norm_real(path), ol.norm_real(self.root))

    def display(self, path: Path) -> str:
        return ol.relative_display(self.root, path) if self.inside(path) else str(path)

    def scope_real(self, entries: list[str]) -> list[str]:
        return [ol.norm_real(self.abs_path(item)) for item in entries]

    def check_write_scope(self, entries: list[str]) -> list[str]:
        root_real = ol.norm_real(self.root)
        protected = self.protected()
        result = []
        for entry in entries:
            path = self.abs_path(entry)
            real = ol.norm_real(path)
            if not ol.path_within(real, root_real):
                raise OrchError(f"写入范围必须在项目内：{entry}")
            if real == root_real:
                raise OrchError("写入范围不能是项目根")
            for item in protected:
                if ol.paths_overlap(real, item):
                    raise OrchError(f"写入范围触及受保护路径：{entry}")
            result.append(self.display(path))
        return result

    def verify_inputs(self, inputs: list[dict[str, str]]) -> None:
        for item in inputs:
            path = self.abs_path(item["path"])
            if not path.is_file():
                raise OrchError(f"输入文件不存在：{item['path']}")
            actual = ol.sha256_file(path)
            if actual != item["sha256"]:
                raise OrchError(f"输入文件哈希不符：{item['path']}（实际 {actual}）")

    # ------------------------------------------------------------ 命令：只读

    def cmd_config(self, _payload) -> dict[str, Any]:
        catalog = ol.catalog_location(self.root)
        return {
            "ok": True,
            "root": str(self.root),
            "project_config": {
                "present": self.cfg["present"], "path": self.cfg["path"], "sha256": self.cfg["sha256"],
            },
            "state_dir": str(self.state_dir),
            "ledger_exists": self.db_path.exists(),
            "protected_paths": [self.display(Path(p)) for p in self.protected()],
            "concurrency": self.limit(),
            "dispatch": self.cfg["dispatch"],
            "agent_prefix": self.cfg["agent_prefix"],
            "has_project_roles": self.cfg["roles"] is not None,
            "startup": self.cfg["startup"],
            "rules": self.cfg["rules"],
            "model_catalog": str(catalog["path"]) if catalog else None,
        }

    def task_view(self, conn, task: sqlite3.Row, brief: bool) -> dict[str, Any]:
        contract = json.loads(task["contract_json"])
        attempts = conn.execute(
            "SELECT * FROM attempts WHERE run_id=? AND task_id=? ORDER BY attempt", (task["run_id"], task["task_id"])
        ).fetchall()
        dispatch = contract["dispatch"]
        view = {
            "task_id": task["task_id"],
            "role": task["role"],
            "status": task["status"],
            "dispatch_mode": dispatch["mode"],
            "agent_type": dispatch["agent_type"],
            "attempts": len([a for a in attempts if a["status"] != "cancelled"]),
            "max_attempts": contract["max_attempts"],
            "live": any(a["live"] for a in attempts),
        }
        if dispatch.get("degraded_reason"):
            view["degraded_reason"] = dispatch["degraded_reason"]
        if brief:
            return view
        view["contract"] = contract
        view["attempt_records"] = [
            {
                "attempt": a["attempt"], "status": a["status"], "live": bool(a["live"]),
                "contract_path": a["contract_path"], "contract_sha256": a["contract_sha256"],
                "agent_handle": a["agent_handle"],
                "spawn": json.loads(a["spawn_json"]) if a["spawn_json"] else None,
                "corrections": a["corrections"],
                "release": json.loads(a["release_json"]) if a["release_json"] else None,
                "decision": json.loads(a["decision_json"]) if a["decision_json"] else None,
                "reason": a["reason"], "started_at": a["started_at"], "updated_at": a["updated_at"],
            }
            for a in attempts
        ]
        return view

    def cmd_status(self, run_id: str | None, brief: bool) -> dict[str, Any]:
        conn = self.open()
        if run_id is None:
            runs = conn.execute("SELECT * FROM runs ORDER BY created_at, run_id").fetchall()
            listing = []
            for run in runs:
                counts: dict[str, int] = {}
                for row in conn.execute("SELECT status FROM tasks WHERE run_id=?", (run["run_id"],)):
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                listing.append({"run_id": run["run_id"], "status": run["status"], "objective": run["objective"], "tasks": counts, "created_at": run["created_at"]})
            live = conn.execute("SELECT COUNT(*) FROM attempts WHERE live=1").fetchone()[0]
            return {"ok": True, "runs": listing, "live": live, "limit": self.limit()["effective"]}
        run = self.get_run(conn, run_id, need_open=False)
        tasks = conn.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        result = {
            "ok": True,
            "run_id": run_id,
            "status": run["status"],
            "tasks": [self.task_view(conn, task, brief) for task in tasks],
        }
        if not brief:
            result.update(
                objective=run["objective"],
                loaded_agents=json.loads(run["loaded_json"]),
                close=json.loads(run["close_json"]) if run["close_json"] else None,
                created_at=run["created_at"],
                updated_at=run["updated_at"],
            )
        return result

    # ------------------------------------------------------------ 命令：run

    def _loaded(self, value: Any) -> dict[str, Any]:
        _require_keys(value, {"observed_from", "names"}, label="loaded_agents")
        _nonempty_str(value["observed_from"], "loaded_agents.observed_from")
        names = value["names"]
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise OrchError("loaded_agents.names 必须是字符串数组（可为空）")
        return {"observed_from": value["observed_from"], "names": sorted(set(names))}

    def _save_loaded(self, run_id: str, loaded: dict[str, Any]) -> str:
        folder = self.run_dir(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "loaded-agents.json"
        index = 1
        while path.exists():
            index += 1
            path = folder / f"loaded-agents-{index}.json"
        ol.write_text_lf(path, ol.pretty_json(dict(loaded, recorded_at=ol.now_local())))
        return str(path)

    def cmd_create_run(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "objective", "loaded_agents"})
        run_id = _check_id(payload["run_id"], "run_id")
        objective = _nonempty_str(payload["objective"], "objective")
        loaded = self._loaded(payload["loaded_agents"])
        self.open(create=True)
        with self.tx() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                raise OrchError(f"run 已存在：{run_id}")
            now = ol.now_local()
            conn.execute(
                "INSERT INTO runs(run_id, objective, status, loaded_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (run_id, objective, "open", ol.canonical_json(loaded), now, now),
            )
            self.event(conn, run_id, None, None, "create-run", {"loaded_agents": loaded})
        saved = self._save_loaded(run_id, loaded)
        return {"ok": True, "run_id": run_id, "loaded_agents": loaded, "loaded_agents_file": saved}

    def cmd_set_loaded_agents(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "loaded_agents"})
        loaded = self._loaded(payload["loaded_agents"])
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            conn.execute(
                "UPDATE runs SET loaded_json=?, updated_at=? WHERE run_id=?",
                (ol.canonical_json(loaded), ol.now_local(), payload["run_id"]),
            )
            self.event(conn, payload["run_id"], None, None, "set-loaded-agents", {"loaded_agents": loaded})
        saved = self._save_loaded(payload["run_id"], loaded)
        return {"ok": True, "run_id": payload["run_id"], "loaded_agents": loaded, "loaded_agents_file": saved}

    # ------------------------------------------------------------ 命令：任务

    def normalize_contract(self, conn, raw: Any, batch_ids: set[str]) -> dict[str, Any]:
        _require_keys(raw, set(REQUIRED_FIELDS), set(OPTIONAL_FIELDS), label="合同")
        run_id = _check_id(raw["run_id"], "run_id")
        task_id = _check_id(raw["task_id"], "task_id")
        role = raw["role"]
        if role not in ol.ROLES:
            raise OrchError(f"未知岗位：{role}（可用：{', '.join(ol.ROLES)}）")
        objective = _nonempty_str(raw["objective"], "objective")

        inputs = raw["inputs"]
        if not isinstance(inputs, list):
            raise OrchError("inputs 必须是数组")
        clean_inputs = []
        for index, item in enumerate(inputs):
            _require_keys(item, {"path", "sha256"}, label=f"inputs[{index}]")
            _nonempty_str(item["path"], f"inputs[{index}].path")
            if not isinstance(item["sha256"], str) or not SHA_RE.match(item["sha256"]):
                raise OrchError(f"inputs[{index}].sha256 必须是 64 位小写十六进制")
            clean_inputs.append({"path": item["path"], "sha256": item["sha256"]})
        self.verify_inputs(clean_inputs)

        read_scope = _str_list(raw["read_scope"], "read_scope")
        write_scope = _str_list(raw["write_scope"], "write_scope")
        if role in ol.READ_ONLY_ROLES and write_scope:
            raise OrchError(f"{role} 是只读岗位，write_scope 必须为空")
        write_scope = self.check_write_scope(write_scope)
        read_display = [self.display(self.abs_path(item)) for item in read_scope]
        added = [item for item in write_scope if item not in read_display]
        read_display += added

        dependencies = _str_list(raw["dependencies"], "dependencies")
        for dep in dependencies:
            if dep == task_id:
                raise OrchError(f"任务 {task_id} 不能依赖自身")
            known = conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND task_id=?", (run_id, dep)).fetchone()
            if not known and dep not in batch_ids:
                raise OrchError(f"任务 {task_id} 的依赖 {dep} 未登记")

        acceptance = _str_list(raw["acceptance"], "acceptance", nonempty=True)
        failure_conditions = _str_list(raw["failure_conditions"], "failure_conditions")
        allowed_calls = _str_list(raw.get("allowed_calls", []), "allowed_calls")
        max_attempts = raw.get("max_attempts", 2)
        if not ol.is_int(max_attempts) or max_attempts not in (1, 2):
            raise OrchError("max_attempts 只能是 1 或 2")
        contract = {
            "contract_schema": ol.CONTRACT_SCHEMA,
            "run_id": run_id,
            "task_id": task_id,
            "role": role,
            "objective": objective,
            "inputs": clean_inputs,
            "read_scope": read_display,
            "read_scope_added_from_write": added,
            "write_scope": write_scope,
            "dependencies": dependencies,
            "acceptance": acceptance,
            "failure_conditions": failure_conditions,
            "baseline_calls": BASELINE_CALLS,
            "allowed_calls": allowed_calls,
            "max_attempts": max_attempts,
            "project_rules": list(self.cfg["rules"]),
        }
        if "execution_instructions" in raw:
            contract["execution_instructions"] = _nonempty_str(raw["execution_instructions"], "execution_instructions")
        if "requested_agent" in raw:
            contract["requested_agent"] = _nonempty_str(raw["requested_agent"], "requested_agent")
        if "revises" in raw:
            rev = _require_keys(raw["revises"], {"run_id", "task_id", "attempt", "reason"}, label="revises")
            if not ol.is_int(rev["attempt"]) or rev["attempt"] < 1:
                raise OrchError("revises.attempt 必须是正整数")
            _nonempty_str(rev["reason"], "revises.reason")
            found = conn.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND task_id=? AND attempt=?",
                (rev["run_id"], rev["task_id"], rev["attempt"]),
            ).fetchone()
            if not found:
                raise OrchError("revises 指向的尝试不存在")
            contract["revises"] = dict(rev)
        return contract

    def attach_dispatch(self, contract: dict[str, Any], run: sqlite3.Row) -> dict[str, Any]:
        dispatch = ol.select_dispatch(
            self.root, self.cfg, contract["role"], self.loaded_names(run), contract.get("requested_agent"),
        )
        contract["dispatch"] = dispatch
        if dispatch["mode"] == "inline":
            contract["role_instructions"] = ol.role_instructions(contract["role"])
        else:
            contract.pop("role_instructions", None)
        return contract

    def cmd_add_task(self, payload) -> dict[str, Any]:
        if isinstance(payload, dict) and set(payload) == {"tasks"}:
            items = payload["tasks"]
            if not isinstance(items, list) or not items:
                raise OrchError("tasks 必须是非空数组")
        else:
            items = [payload]
        run_ids = {item.get("run_id") if isinstance(item, dict) else None for item in items}
        if len(run_ids) != 1:
            raise OrchError("批量 add-task 中所有合同必须属于同一个 run")
        run_id = run_ids.pop()
        batch_ids = [item.get("task_id") for item in items]
        if len(set(batch_ids)) != len(batch_ids):
            raise OrchError("批量 add-task 中 task_id 重复")
        added = []
        with self.tx() as conn:
            run = self.get_run(conn, _check_id(run_id, "run_id"))
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM tasks WHERE run_id=?", (run_id,)).fetchone()[0]
            contracts = []
            for item in items:
                contract = self.normalize_contract(conn, item, set(batch_ids))
                if conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND task_id=?", (run_id, contract["task_id"])).fetchone():
                    raise OrchError(f"任务已存在：{contract['task_id']}")
                contracts.append(self.attach_dispatch(contract, run))
            graph = {c["task_id"]: [d for d in c["dependencies"] if d in batch_ids] for c in contracts}
            visiting, done = set(), set()

            def visit(node):
                if node in done:
                    return
                if node in visiting:
                    raise OrchError(f"依赖存在环：{node}")
                visiting.add(node)
                for nxt in graph.get(node, []):
                    visit(nxt)
                visiting.discard(node)
                done.add(node)

            for node in graph:
                visit(node)
            now = ol.now_local()
            for contract in contracts:
                seq += 1
                conn.execute(
                    "INSERT INTO tasks(run_id, task_id, seq, role, status, contract_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, contract["task_id"], seq, contract["role"], "planned", ol.canonical_json(contract), now, now),
                )
                self.event(conn, run_id, contract["task_id"], None, "add-task", {"dispatch": contract["dispatch"]})
                added.append({
                    "task_id": contract["task_id"],
                    "role": contract["role"],
                    "dispatch": contract["dispatch"],
                })
        degraded = [item for item in added if item["dispatch"]["mode"] == "inline"]
        result = {"ok": True, "run_id": run_id, "tasks": added}
        if degraded:
            result["notice"] = "部分任务降级为 inline 派发（默认子代理 + 合同携带岗位职责），请告知用户：" + "；".join(
                item["dispatch"]["degraded_reason"] for item in degraded
            )
        return result

    def cmd_rebind(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id"}, {"reason"})
        with self.tx() as conn:
            run = self.get_run(conn, payload["run_id"])
            task = self.get_task(conn, payload["run_id"], payload["task_id"])
            if task["status"] != "planned" or self.counted_attempts(conn, task["run_id"], task["task_id"]):
                raise OrchError("rebind 只适用于从未开始过的任务")
            contract = json.loads(task["contract_json"])
            before = contract["dispatch"]
            contract["project_rules"] = list(self.cfg["rules"])
            self.attach_dispatch(contract, run)
            conn.execute(
                "UPDATE tasks SET contract_json=?, updated_at=? WHERE run_id=? AND task_id=?",
                (ol.canonical_json(contract), ol.now_local(), task["run_id"], task["task_id"]),
            )
            self.event(conn, task["run_id"], task["task_id"], None, "rebind", {"before": before, "after": contract["dispatch"], "reason": payload.get("reason")})
        return {"ok": True, "task_id": task["task_id"], "before": before, "dispatch": contract["dispatch"]}

    def contract_file(self, run_id: str, task_id: str, attempt: int) -> Path:
        return self.state_dir / "contracts" / run_id / f"{task_id}-attempt-{attempt}.json"

    def dispatch_message(self, contract, attempt, path, digest) -> str:
        dispatch = contract["dispatch"]
        who = dispatch["agent_type"] if dispatch["mode"] == "named" else "default subagent (inline)"
        lines = [
            f"Role: {contract['role']} ({who})",
            f"run={contract['run_id']} task={contract['task_id']} attempt={attempt}",
            f"Authoritative contract: {path}",
            f"Contract SHA-256: {digest}",
            "Read this contract first and verify its SHA-256 and every inputs hash; on any mismatch, stop and report.",
        ]
        if dispatch["mode"] == "inline":
            lines.append("Your role instructions are in the contract's role_instructions field; follow them in full.")
        lines.append("Execute the contract; your final reply must contain exactly one JSON delivery object.")
        return "\n".join(lines)

    def cmd_start(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id"})
        with self.tx() as conn:
            run = self.get_run(conn, payload["run_id"])
            task = self.get_task(conn, payload["run_id"], payload["task_id"])
            if task["status"] != "planned":
                raise OrchError(f"任务状态为 {task['status']}，只有 planned 可以 start")
            contract = json.loads(task["contract_json"])
            for dep in contract["dependencies"]:
                dep_row = self.get_task(conn, run["run_id"], dep)
                if dep_row["status"] != "accepted":
                    raise OrchError(f"依赖 {dep} 尚未 accepted（当前 {dep_row['status']}）")
            counted = self.counted_attempts(conn, run["run_id"], task["task_id"])
            if counted >= contract["max_attempts"]:
                raise OrchError("已达尝试上限")
            live = conn.execute("SELECT COUNT(*) FROM attempts WHERE live=1").fetchone()[0]
            limit = self.limit()["effective"]
            if live >= limit:
                raise OrchError(f"并发名额已满（{live}/{limit}），等待其他任务 release")
            mine = self.scope_real(contract["write_scope"])
            others = conn.execute(
                "SELECT a.run_id, a.task_id, t.contract_json FROM attempts a JOIN tasks t ON a.run_id=t.run_id AND a.task_id=t.task_id WHERE a.live=1"
            ).fetchall()
            for other in others:
                theirs = self.scope_real(json.loads(other["contract_json"])["write_scope"])
                for first in mine:
                    if any(ol.paths_overlap(first, second) for second in theirs):
                        raise OrchError(f"写入范围与进行中的 {other['run_id']}/{other['task_id']} 重叠")
            self.verify_inputs(contract["inputs"])
            current = ol.select_dispatch(
                self.root, self.cfg, contract["role"], self.loaded_names(run), contract.get("requested_agent"),
            )
            if ol.dispatch_fingerprint(current) != ol.dispatch_fingerprint(contract["dispatch"]):
                hint = "未开始过的任务可用 rebind 重新选择" if counted == 0 else "请另立修订合同（revises）"
                raise OrchError(f"岗位选择或其配置已变化，拒绝 start；{hint}")
            if contract["project_rules"] != self.cfg["rules"]:
                raise OrchError("项目 rules 已变化，拒绝 start；未开始过的任务可用 rebind")
            attempt_no = (self.last_attempt(conn, run["run_id"], task["task_id"]) or {"attempt": 0})["attempt"] + 1
            exported = dict(contract, attempt=attempt_no)
            data = ol.pretty_json(exported).encode("utf-8")
            path = self.contract_file(run["run_id"], task["task_id"], attempt_no)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, "xb") as handle:
                    handle.write(data)
                reused = False
            except FileExistsError:
                if path.read_bytes() != data:
                    raise OrchError(f"合同文件已存在且内容不同，拒绝覆盖：{path}")
                reused = True
            digest = ol.sha256_bytes(data)
            now = ol.now_local()
            conn.execute(
                "INSERT INTO attempts(run_id, task_id, attempt, status, live, contract_path, contract_sha256, started_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run["run_id"], task["task_id"], attempt_no, "started", 1, str(path), digest, now, now),
            )
            self.set_task(conn, run["run_id"], task["task_id"], "running")
            self.event(conn, run["run_id"], task["task_id"], attempt_no, "start", {"contract_sha256": digest, "reused_file": reused})
        dispatch = contract["dispatch"]
        return {
            "ok": True,
            "task_id": task["task_id"],
            "attempt": attempt_no,
            "contract_path": str(path),
            "contract_sha256": digest,
            "dispatch": {
                "mode": dispatch["mode"],
                "agent_type": dispatch["agent_type"],
                "degraded_reason": dispatch["degraded_reason"],
            },
            "spawn_hint": "named 传 agent_type，inline 不传；v1 用 fork_context=false，v2 用 fork_turns=\"none\"；不传 model 或 reasoning_effort",
            "message": self.dispatch_message(contract, attempt_no, path, digest),
        }

    def current_attempt(self, conn, run_id, task_id, statuses: tuple[str, ...]) -> sqlite3.Row:
        row = self.last_attempt(conn, run_id, task_id)
        if row is None or row["status"] not in statuses:
            have = row["status"] if row else "无尝试"
            raise OrchError(f"当前尝试状态为 {have}，此操作要求 {'/'.join(statuses)}")
        return row

    def cmd_cancel_start(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id", "reason"})
        _nonempty_str(payload["reason"], "reason")
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            row = self.current_attempt(conn, payload["run_id"], payload["task_id"], ("started",))
            self.set_attempt(conn, row, status="cancelled", live=0, reason=payload["reason"])
            self.set_task(conn, row["run_id"], row["task_id"], "planned")
            self.event(conn, row["run_id"], row["task_id"], row["attempt"], "cancel-start", {"reason": payload["reason"]})
        return {"ok": True, "task_id": row["task_id"], "attempt": row["attempt"], "status": "cancelled", "counted": False}

    def cmd_bind(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id", "agent_handle", "spawn"})
        handle = _nonempty_str(payload["agent_handle"], "agent_handle")
        spawn = _require_keys(
            payload["spawn"],
            {"tool_version", "agent_type", "inherits_context", "model_override", "reasoning_effort_override"},
            label="spawn",
        )
        if spawn["tool_version"] not in ("v1", "v2"):
            raise OrchError("spawn.tool_version 只能是 v1 或 v2")
        if spawn["inherits_context"] is not False:
            raise OrchError("子代理不得继承父会话上下文（v1 fork_context=false，v2 fork_turns=\"none\"）")
        if spawn["model_override"] is not None or spawn["reasoning_effort_override"] is not None:
            raise OrchError("派发时不得覆盖 model 或 reasoning_effort")
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            task = self.get_task(conn, payload["run_id"], payload["task_id"])
            row = self.last_attempt(conn, payload["run_id"], payload["task_id"])
            if row is None or row["status"] != "started" or row["agent_handle"]:
                raise OrchError("只能对刚 start、尚未绑定的尝试 bind，每次尝试只能绑定一次")
            dispatch = json.loads(task["contract_json"])["dispatch"]
            expected = dispatch["agent_type"] if dispatch["mode"] == "named" else None
            if spawn["agent_type"] != expected:
                raise OrchError(f"实际 agent_type {spawn['agent_type']!r} 与合同派发 {expected!r} 不符")
            self.set_attempt(conn, row, status="bound", agent_handle=handle, spawn_json=ol.canonical_json(spawn))
            self.event(conn, row["run_id"], row["task_id"], row["attempt"], "bind", {"agent_handle": handle, "spawn": spawn})
        return {"ok": True, "task_id": row["task_id"], "attempt": row["attempt"], "agent_handle": handle}

    # ------------------------------------------------------------ 交付检查

    def parse_delivery(self, payload) -> tuple[str | None, Any, list[str]]:
        sources = [key for key in ("reply", "reply_path", "delivery") if key in payload]
        if len(sources) != 1:
            raise OrchError("须且只能提供 reply、reply_path、delivery 之一")
        source = sources[0]
        if source == "delivery":
            return None, payload["delivery"], []
        if source == "reply_path":
            path = self.abs_path(_nonempty_str(payload["reply_path"], "reply_path"))
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise OrchError(f"无法读取回复原文 {path}：{exc}") from exc
        else:
            text = payload["reply"]
            if not isinstance(text, str):
                raise OrchError("reply 必须是字符串")
        body = text.strip()
        fence = re.match(r"^```(?:json)?\s*\n(.*)\n```$", body, re.S)
        if fence:
            body = fence.group(1)
        try:
            return text, ol.loads_strict(body, "交付"), []
        except OrchError as exc:
            return text, None, [f"回复不是单个 JSON 交付对象：{exc}"]

    def check_delivery(self, delivery: Any, contract: dict[str, Any], attempt: sqlite3.Row) -> list[str]:
        problems: list[str] = []
        if not isinstance(delivery, dict):
            return ["交付必须是 JSON 对象"]
        role = contract["role"]
        unknown = set(delivery) - DELIVERY_KEYS
        if unknown:
            problems.append(f"交付含未知字段：{', '.join(sorted(unknown))}")
        for key in ("contract_receipt", "operations", "artifacts", "checks", "unresolved", "observations"):
            if key not in delivery:
                problems.append(f"交付缺少字段 {key}")
        if role == "reviewer" and "findings" not in delivery:
            problems.append("reviewer 交付缺少 findings（无问题时为空数组）")

        receipt = delivery.get("contract_receipt")
        if isinstance(receipt, dict):
            if set(receipt) != {"path", "sha256", "inputs_verified"}:
                problems.append("contract_receipt 须且只能包含 path、sha256、inputs_verified")
            path = receipt.get("path")
            if not isinstance(path, str) or ol.norm_real(path) != ol.norm_real(attempt["contract_path"]):
                problems.append("contract_receipt.path 与本次导出的合同不一致")
            if receipt.get("sha256") != attempt["contract_sha256"]:
                problems.append("contract_receipt.sha256 与本次导出的合同不一致")
            if receipt.get("inputs_verified") is not True:
                problems.append("contract_receipt.inputs_verified 必须为 true")
        elif "contract_receipt" in delivery:
            problems.append("contract_receipt 必须是对象")

        operations = delivery.get("operations")
        if not isinstance(operations, list) or not operations:
            problems.append("operations 必须是非空数组")
        else:
            for index, op in enumerate(operations):
                if not isinstance(op, dict) or set(op) != {"action", "exit_code", "result"}:
                    problems.append(f"operations[{index}] 须且只能包含 action、exit_code、result")
                    continue
                if not isinstance(op["action"], str) or not op["action"].strip():
                    problems.append(f"operations[{index}].action 必须是非空字符串")
                if op["exit_code"] is not None and not ol.is_int(op["exit_code"]):
                    problems.append(f"operations[{index}].exit_code 必须是整数或 null")
                if not isinstance(op["result"], str):
                    problems.append(f"operations[{index}].result 必须是字符串")

        artifacts = delivery.get("artifacts")
        if not isinstance(artifacts, list):
            problems.append("artifacts 必须是数组")
        else:
            for index, art in enumerate(artifacts):
                if not isinstance(art, dict) or set(art) != {"path", "sha256"}:
                    problems.append(f"artifacts[{index}] 须且只能包含 path、sha256")
                elif not isinstance(art["path"], str) or not isinstance(art["sha256"], str) or not SHA_RE.match(art["sha256"]):
                    problems.append(f"artifacts[{index}] 的 path 或 sha256 格式不正确")
            if role in ol.READ_ONLY_ROLES and artifacts:
                problems.append(f"{role} 是只读岗位，artifacts 必须为空")

        checks = delivery.get("checks")
        any_failed = False
        if not isinstance(checks, list) or not checks:
            problems.append("checks 必须是非空数组")
        else:
            for index, check in enumerate(checks):
                if not isinstance(check, dict) or set(check) != {"name", "passed", "evidence"}:
                    problems.append(f"checks[{index}] 须且只能包含 name、passed、evidence")
                    continue
                if not isinstance(check["name"], str) or not check["name"].strip():
                    problems.append(f"checks[{index}].name 必须是非空字符串")
                if not isinstance(check["passed"], bool):
                    problems.append(f"checks[{index}].passed 必须是布尔值")
                elif not check["passed"]:
                    any_failed = True
                if not isinstance(check["evidence"], str) or not check["evidence"].strip():
                    problems.append(f"checks[{index}].evidence 必须是非空字符串")

        unresolved = delivery.get("unresolved")
        if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
            problems.append("unresolved 必须是字符串数组")
            unresolved = []
        observations = delivery.get("observations")
        if not isinstance(observations, list) or not all(isinstance(item, str) for item in observations):
            problems.append("observations 必须是字符串数组")
        if any_failed and not unresolved:
            problems.append("存在 passed=false 的检查，但 unresolved 为空")
        if unresolved and isinstance(checks, list) and checks and not any_failed:
            problems.append("unresolved 非空，但全部检查都标为通过，前后矛盾")

        findings = delivery.get("findings")
        if role != "reviewer":
            if findings:
                problems.append("只有 reviewer 可以提交非空 findings")
        elif findings is not None:
            if not isinstance(findings, list):
                problems.append("findings 必须是数组")
            else:
                for index, finding in enumerate(findings):
                    keys = {"location", "trigger", "impact", "evidence", "blocking"}
                    if not isinstance(finding, dict) or set(finding) != keys:
                        problems.append(f"findings[{index}] 须且只能包含 {', '.join(sorted(keys))}")
                        continue
                    for key in ("location", "trigger", "impact", "evidence"):
                        if not isinstance(finding[key], str) or not finding[key].strip():
                            problems.append(f"findings[{index}].{key} 必须是非空字符串")
                    if not isinstance(finding["blocking"], bool):
                        problems.append(f"findings[{index}].blocking 必须是布尔值")
        return problems

    def check_artifacts(self, delivery: dict[str, Any], contract: dict[str, Any]) -> list[str]:
        problems = []
        scopes = self.scope_real(contract["write_scope"])
        for art in delivery.get("artifacts") or []:
            path = self.abs_path(art["path"])
            real = ol.norm_real(path)
            if not any(ol.path_within(real, scope) for scope in scopes):
                problems.append(f"产物不在写入范围内：{art['path']}")
                continue
            if not path.is_file():
                problems.append(f"产物文件不存在：{art['path']}")
            elif ol.sha256_file(path) != art["sha256"]:
                problems.append(f"产物哈希与磁盘不一致：{art['path']}")
        return problems

    def cmd_check_report(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id"}, {"reply", "reply_path", "delivery"})
        conn = self.open()
        task = self.get_task(conn, payload["run_id"], payload["task_id"])
        row = self.last_attempt(conn, payload["run_id"], payload["task_id"])
        if row is None:
            raise OrchError("任务还没有尝试")
        contract = json.loads(task["contract_json"])
        _text, delivery, problems = self.parse_delivery(payload)
        if delivery is not None:
            problems += self.check_delivery(delivery, contract, row)
            if isinstance(delivery, dict) and isinstance(delivery.get("artifacts"), list) and not problems:
                problems += self.check_artifacts(delivery, contract)
        return {"ok": not problems, "task_id": row["task_id"], "attempt": row["attempt"], "problems": problems}

    def save_reply(self, row, text: str | None, delivery: Any) -> dict[str, str]:
        folder = self.run_dir(row["run_id"])
        folder.mkdir(parents=True, exist_ok=True)
        version = row["corrections"] + 1
        stem = f"{row['task_id']}-attempt-{row['attempt']}"
        saved = {}
        if text is not None:
            reply_path = folder / f"{stem}.reply-{version}.txt"
            ol.write_text_lf(reply_path, text)
            saved["reply"] = str(reply_path)
        if delivery is not None:
            delivery_path = folder / f"{stem}.delivery-{version}.json"
            ol.write_text_lf(delivery_path, ol.pretty_json(delivery))
            saved["delivery"] = str(delivery_path)
        return saved

    def cmd_report(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id"}, {"reply", "reply_path", "delivery"})
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            task = self.get_task(conn, payload["run_id"], payload["task_id"])
            row = self.current_attempt(conn, payload["run_id"], payload["task_id"], ("bound",))
            contract = json.loads(task["contract_json"])
            text, delivery, problems = self.parse_delivery(payload)
            if delivery is not None:
                problems += self.check_delivery(delivery, contract, row)
                if not problems:
                    problems += self.check_artifacts(delivery, contract)
            saved = self.save_reply(row, text, delivery)
            if problems:
                if row["corrections"] == 0:
                    self.set_attempt(conn, row, corrections=1)
                    self.event(conn, row["run_id"], row["task_id"], row["attempt"], "report-rejected", {"problems": problems, "saved": saved})
                    return {
                        "ok": False,
                        "task_id": row["task_id"],
                        "attempt": row["attempt"],
                        "problems": problems,
                        "saved": saved,
                        "next": "请用补充消息让同一代理更正一次交付（不计新尝试），再次 report；仍不一致将记为 failed",
                    }
                reason = "更正后的交付仍不一致：" + "；".join(problems)
                self.set_attempt(conn, row, status="failed", corrections=row["corrections"] + 1, reason=reason)
                self.set_task(conn, row["run_id"], row["task_id"], "failed")
                self.event(conn, row["run_id"], row["task_id"], row["attempt"], "report-failed", {"problems": problems, "saved": saved})
                return {
                    "ok": False, "task_id": row["task_id"], "attempt": row["attempt"], "status": "failed",
                    "problems": problems, "saved": saved, "next": "关闭代理并确认终止后 release，再决定 retry 或 close-run",
                }
            self.set_attempt(conn, row, status="reported", delivery_json=ol.canonical_json(delivery))
            self.set_task(conn, row["run_id"], row["task_id"], "reported")
            self.event(conn, row["run_id"], row["task_id"], row["attempt"], "report", {"saved": saved})
        return {"ok": True, "task_id": row["task_id"], "attempt": row["attempt"], "status": "reported", "saved": saved,
                "next": "关闭代理并确认终止后 release，主控独立复核后 accept 或 fail"}

    def cmd_release(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id", "terminal_confirmed", "basis"})
        if payload["terminal_confirmed"] is not True:
            raise OrchError("release 要求 terminal_confirmed 为 true")
        basis = _nonempty_str(payload["basis"], "basis")
        with self.tx() as conn:
            row = self.current_attempt(
                conn, payload["run_id"], payload["task_id"], ("bound", "reported", "failed", "interrupted"),
            )
            if not row["live"]:
                raise OrchError("该尝试已经 release")
            info = {"terminal_confirmed": True, "basis": basis, "at": ol.now_local()}
            self.set_attempt(conn, row, live=0, release_json=ol.canonical_json(info))
            self.event(conn, row["run_id"], row["task_id"], row["attempt"], "release", info)
        return {"ok": True, "task_id": row["task_id"], "attempt": row["attempt"], "released": True}

    def _end_attempt(self, payload, status: str) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id", "reason"})
        reason = _nonempty_str(payload["reason"], "reason")
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            row = self.current_attempt(conn, payload["run_id"], payload["task_id"], ("started", "bound", "reported"))
            self.set_attempt(conn, row, status=status, reason=reason)
            self.set_task(conn, row["run_id"], row["task_id"], status)
            self.event(conn, row["run_id"], row["task_id"], row["attempt"], status, {"reason": reason})
        return {"ok": True, "task_id": row["task_id"], "attempt": row["attempt"], "status": status,
                "live": bool(row["live"]), "next": "确认代理终止后 release；写入占用在 release 前保持"}

    def cmd_fail(self, payload) -> dict[str, Any]:
        return self._end_attempt(payload, "failed")

    def cmd_interrupt(self, payload) -> dict[str, Any]:
        return self._end_attempt(payload, "interrupted")

    def cmd_retry(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id", "reason"})
        reason = _nonempty_str(payload["reason"], "reason")
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            task = self.get_task(conn, payload["run_id"], payload["task_id"])
            if task["status"] not in ("failed", "interrupted"):
                raise OrchError(f"只有 failed 或 interrupted 的任务可以 retry（当前 {task['status']}）")
            row = self.last_attempt(conn, task["run_id"], task["task_id"])
            if row["live"]:
                raise OrchError("上一次尝试尚未 release，不能 retry")
            contract = json.loads(task["contract_json"])
            counted = self.counted_attempts(conn, task["run_id"], task["task_id"])
            if counted >= contract["max_attempts"]:
                raise OrchError(f"已用完 {contract['max_attempts']} 次尝试，不能 retry；如实保留失败，必要时 close-run 或另立修订合同")
            self.set_task(conn, task["run_id"], task["task_id"], "planned")
            self.event(conn, task["run_id"], task["task_id"], row["attempt"], "retry", {"reason": reason})
        return {"ok": True, "task_id": task["task_id"], "status": "planned", "attempts_used": counted,
                "next": "再次 start 会导出新的合同文件，需要重新派发并 bind"}

    def pin_files(self, entries: Any, label: str) -> list[dict[str, str]]:
        if not ol.is_str_list(entries):
            raise OrchError(f"{label} 必须是路径字符串数组")
        pinned = []
        for entry in entries:
            path = self.abs_path(entry)
            if not path.is_file():
                raise OrchError(f"{label} 文件不存在：{entry}")
            pinned.append({"path": self.display(path), "sha256": ol.sha256_file(path)})
        return pinned

    def cmd_accept(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "task_id", "decision"})
        decision = _require_keys(payload["decision"], {"checks", "evidence", "unresolved"}, {"findings"}, label="decision")
        with self.tx() as conn:
            self.get_run(conn, payload["run_id"])
            task = self.get_task(conn, payload["run_id"], payload["task_id"])
            row = self.current_attempt(conn, payload["run_id"], payload["task_id"], ("reported",))
            if row["live"]:
                raise OrchError("请先关闭代理、确认终止并 release，再 accept")
            contract = json.loads(task["contract_json"])
            delivery = json.loads(row["delivery_json"])
            failed = [c["name"] for c in delivery["checks"] if not c["passed"]]
            if failed or delivery["unresolved"]:
                raise OrchError("交付中有未通过的检查或未解决问题，不能 accept；请 fail 后按流程返工")
            drift = self.check_artifacts(delivery, contract)
            if drift:
                raise OrchError("产物在交付后发生变化：" + "；".join(drift))
            checks = decision["checks"]
            if not isinstance(checks, list) or not checks:
                raise OrchError("decision.checks 必须是非空数组")
            for index, check in enumerate(checks):
                _require_keys(check, {"name", "passed", "evidence"}, label=f"decision.checks[{index}]")
                if check["passed"] is not True:
                    raise OrchError(f"主控检查 {check.get('name')} 未通过，不能 accept")
                _nonempty_str(check["evidence"], f"decision.checks[{index}].evidence")
            if decision["unresolved"]:
                raise OrchError("decision.unresolved 非空，不能 accept")
            evidence = self.pin_files(decision["evidence"], "decision.evidence")
            if not evidence:
                raise OrchError("decision.evidence 至少包含一个证据文件")
            findings = delivery.get("findings") or []
            dispositions = decision.get("findings", [])
            if contract["role"] == "reviewer":
                if not isinstance(dispositions, list) or len(dispositions) != len(findings):
                    raise OrchError(f"须逐条处置 reviewer 的 {len(findings)} 条 findings")
                seen = set()
                for index, item in enumerate(dispositions):
                    _require_keys(item, {"index", "disposition", "evidence"}, {"followup_task_id"}, label=f"decision.findings[{index}]")
                    pos = item["index"]
                    if not ol.is_int(pos) or not 0 <= pos < len(findings) or pos in seen:
                        raise OrchError(f"decision.findings[{index}].index 无效或重复")
                    seen.add(pos)
                    if item["disposition"] not in ("confirmed", "rejected"):
                        raise OrchError("disposition 只能是 confirmed 或 rejected")
                    _nonempty_str(item["evidence"], f"decision.findings[{index}].evidence")
                    if item["disposition"] == "confirmed" and findings[pos]["blocking"]:
                        follow = item.get("followup_task_id")
                        if not follow or follow == task["task_id"]:
                            raise OrchError(f"已确认的阻断 finding #{pos} 必须指向同一 run 中的修复任务 followup_task_id")
                        self.get_task(conn, task["run_id"], follow)
            elif dispositions:
                raise OrchError("只有 reviewer 任务的 decision 可以处置 findings")
            record = dict(decision, evidence=evidence, accepted_at=ol.now_local())
            folder = self.run_dir(row["run_id"])
            decision_path = folder / f"{row['task_id']}-attempt-{row['attempt']}.decision.json"
            ol.write_text_lf(decision_path, ol.pretty_json(record))
            self.set_attempt(conn, row, status="accepted", decision_json=ol.canonical_json(record))
            self.set_task(conn, row["run_id"], row["task_id"], "accepted")
            self.event(conn, row["run_id"], row["task_id"], row["attempt"], "accept", {"decision_file": str(decision_path)})
        return {"ok": True, "task_id": row["task_id"], "attempt": row["attempt"], "status": "accepted", "decision_file": str(decision_path)}

    def cmd_finish(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id"}, {"report"})
        with self.tx() as conn:
            run = self.get_run(conn, payload["run_id"])
            tasks = conn.execute("SELECT task_id, status FROM tasks WHERE run_id=? ORDER BY seq", (run["run_id"],)).fetchall()
            if not tasks:
                raise OrchError("run 中没有任务")
            pending = [f"{t['task_id']}({t['status']})" for t in tasks if t["status"] != "accepted"]
            if pending:
                raise OrchError("仍有未 accepted 的任务：" + ", ".join(pending))
            report = self.pin_files([payload["report"]], "report") if "report" in payload else []
            info = {"finished_at": ol.now_local(), "report": report[0] if report else None}
            conn.execute("UPDATE runs SET status='finished', close_json=?, updated_at=? WHERE run_id=?",
                         (ol.canonical_json(info), ol.now_local(), run["run_id"]))
            self.event(conn, run["run_id"], None, None, "finish", info)
        summary = self.cmd_status(run["run_id"], brief=True)
        path = self.run_dir(run["run_id"]) / "journal-summary.json"
        ol.write_text_lf(path, ol.pretty_json(summary))
        return {"ok": True, "run_id": run["run_id"], "status": "finished", "summary_file": str(path)}

    def cmd_close_run(self, payload) -> dict[str, Any]:
        _require_keys(payload, {"run_id", "reason", "evidence"})
        reason = _nonempty_str(payload["reason"], "reason")
        with self.tx() as conn:
            run = self.get_run(conn, payload["run_id"])
            live = conn.execute("SELECT task_id FROM attempts WHERE run_id=? AND live=1", (run["run_id"],)).fetchall()
            if live:
                raise OrchError("仍有未 release 的尝试：" + ", ".join(r["task_id"] for r in live))
            busy = conn.execute(
                "SELECT task_id, status FROM tasks WHERE run_id=? AND status IN ('running','reported')", (run["run_id"],)
            ).fetchall()
            if busy:
                raise OrchError("仍有执行中或待验收的任务：" + ", ".join(f"{r['task_id']}({r['status']})" for r in busy))
            evidence = self.pin_files(payload["evidence"], "evidence")
            info = {"reason": reason, "evidence": evidence, "closed_at": ol.now_local()}
            conn.execute("UPDATE runs SET status='closed_with_failures', close_json=?, updated_at=? WHERE run_id=?",
                         (ol.canonical_json(info), ol.now_local(), run["run_id"]))
            self.event(conn, run["run_id"], None, None, "close-run", info)
        return {"ok": True, "run_id": run["run_id"], "status": "closed_with_failures"}


COMMANDS = {
    "create-run": "cmd_create_run",
    "set-loaded-agents": "cmd_set_loaded_agents",
    "add-task": "cmd_add_task",
    "rebind": "cmd_rebind",
    "start": "cmd_start",
    "cancel-start": "cmd_cancel_start",
    "bind": "cmd_bind",
    "check-report": "cmd_check_report",
    "report": "cmd_report",
    "release": "cmd_release",
    "accept": "cmd_accept",
    "fail": "cmd_fail",
    "interrupt": "cmd_interrupt",
    "retry": "cmd_retry",
    "finish": "cmd_finish",
    "close-run": "cmd_close_run",
}


def read_payload(path: str | None) -> Any:
    if path:
        return ol.read_json_file(path, "输入")
    text = sys.stdin.buffer.read().decode("utf-8-sig")
    if not text.strip():
        raise OrchError("没有输入：用 --input 指定 JSON 文件或从 stdin 传入")
    return ol.loads_strict(text, "输入")


def main(argv: list[str] | None = None) -> int:
    ol.force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="journal.py", description="多代理编排账本")
    parser.add_argument("--root", required=True, help="项目根目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="只读显示生效设置")
    p_status = sub.add_parser("status", help="查看 run 或任务状态")
    p_status.add_argument("--run-id")
    p_status.add_argument("--brief", action="store_true")
    for name in COMMANDS:
        sub.add_parser(name).add_argument("--input", help="JSON 输入文件；省略时读 stdin")
    args = parser.parse_args(argv)
    journal = None
    try:
        journal = Journal(Path(args.root))
        if args.command == "config":
            result = journal.cmd_config(None)
        elif args.command == "status":
            result = journal.cmd_status(args.run_id, args.brief)
        else:
            payload = read_payload(args.input)
            result = getattr(journal, COMMANDS[args.command])(payload)
    except OrchError as exc:
        ol.emit({"ok": False, "error": str(exc)})
        return 1
    finally:
        if journal is not None:
            journal.close()
    ol.emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

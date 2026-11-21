import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "multi-agent-orchestration" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import journal  # noqa: E402
import orchlib  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Env:
    """临时项目 + 临时 CODEX_HOME，并提供账本命令的调用助手。"""

    def __init__(self, case: unittest.TestCase):
        self.case = case
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "proj"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        patcher.start()
        case.addCleanup(patcher.stop)
        self.counter = 0

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def call(self, command: str, payload=None, extra=(), ok=True):
        args = ["--root", str(self.root), command, *extra]
        if payload is not None:
            self.counter += 1
            path = self.base / f"in-{self.counter}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            args += ["--input", str(path)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = journal.main(args)
        result = json.loads(buf.getvalue())
        if ok is True:
            self.case.assertEqual(code, 0, result)
        elif ok is False:
            self.case.assertNotEqual(code, 0, result)
        return result

    def fail(self, command, payload=None, contains=None, extra=()):
        result = self.call(command, payload, extra=extra, ok=False)
        if contains:
            text = result.get("error") or json.dumps(result.get("problems"), ensure_ascii=False)
            self.case.assertIn(contains, text)
        return result

    def run(self, run_id="r1", names=()):
        return self.call("create-run", {
            "run_id": run_id, "objective": "测试", "loaded_agents": {"observed_from": "单元测试", "names": list(names)},
        })

    def contract(self, task_id, role="worker", write=(), deps=(), run_id="r1", **extra):
        base = {
            "run_id": run_id, "task_id": task_id, "role": role, "objective": f"完成 {task_id}",
            "inputs": [], "read_scope": [], "write_scope": list(write), "dependencies": list(deps),
            "acceptance": ["完成"], "failure_conditions": ["越界"],
        }
        base.update(extra)
        return base

    def add(self, task_id, **kwargs):
        return self.call("add-task", self.contract(task_id, **kwargs))

    def start(self, task_id, run_id="r1"):
        return self.call("start", {"run_id": run_id, "task_id": task_id})

    def bind(self, task_id, run_id="r1", agent_type=None, **spawn):
        body = {"tool_version": "v1", "agent_type": agent_type, "inherits_context": False,
                "model_override": None, "reasoning_effort_override": None}
        body.update(spawn)
        return self.call("bind", {"run_id": run_id, "task_id": task_id, "agent_handle": f"agent-{task_id}", "spawn": body},
                         ok=None)

    def delivery(self, started, artifacts=(), checks=None, unresolved=(), findings=None, **overrides):
        body = {
            "contract_receipt": {"path": started["contract_path"], "sha256": started["contract_sha256"], "inputs_verified": True},
            "operations": [{"action": "读取合同", "exit_code": 0, "result": "哈希一致"}],
            "artifacts": [{"path": rel, "sha256": orchlib.sha256_file(self.root / rel)} for rel in artifacts],
            "checks": checks or [{"name": "完成", "passed": True, "evidence": "已核对"}],
            "unresolved": list(unresolved),
            "observations": [],
        }
        if findings is not None:
            body["findings"] = findings
        body.update(overrides)
        return body

    def report(self, task_id, delivery, run_id="r1", ok=True):
        return self.call("report", {"run_id": run_id, "task_id": task_id, "reply": json.dumps(delivery, ensure_ascii=False)}, ok=ok)

    def release(self, task_id, run_id="r1"):
        return self.call("release", {"run_id": run_id, "task_id": task_id, "terminal_confirmed": True, "basis": "close_agent 返回 completed"})

    def evidence(self, name="evidence.txt"):
        return str(self.write(f"notes/{name}", "主控复核记录"))

    def accept(self, task_id, run_id="r1", findings=None, ok=True):
        decision = {"checks": [{"name": "复核", "passed": True, "evidence": "已运行"}], "evidence": [self.evidence()], "unresolved": []}
        if findings is not None:
            decision["findings"] = findings
        return self.call("accept", {"run_id": run_id, "task_id": task_id, "decision": decision}, ok=ok)

    def complete(self, task_id, run_id="r1", files=(), findings=None, dispositions=None):
        started = self.start(task_id, run_id)
        self.case.assertEqual(self.bind(task_id, run_id)["ok"], True)
        for rel in files:
            self.write(rel, f"content of {rel}")
        self.report(task_id, self.delivery(started, artifacts=files, findings=findings), run_id)
        self.release(task_id, run_id)
        return self.accept(task_id, run_id, findings=dispositions)


class LedgerBasics(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_state_dir_gitignore_and_persistence(self):
        self.env.run()
        self.assertEqual((self.env.root / ".orchestration" / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.env.add("t1", write=["src/a.py"])
        status = self.env.call("status", extra=["--run-id", "r1", "--brief"])
        self.assertEqual(status["tasks"][0]["status"], "planned")
        listing = self.env.call("status")
        self.assertEqual(listing["runs"][0]["run_id"], "r1")

    def test_status_without_ledger_does_not_create(self):
        self.env.fail("status", contains="账本不存在")
        self.assertFalse((self.env.root / ".orchestration").exists())

    def test_rejects_foreign_sqlite_and_other_version(self):
        state = self.env.root / ".orchestration"
        state.mkdir()
        conn = sqlite3.connect(state / "state.sqlite3")
        conn.execute("CREATE TABLE x(a)")
        conn.commit()
        conn.close()
        self.env.fail("create-run", {"run_id": "r1", "objective": "x", "loaded_agents": {"observed_from": "t", "names": []}},
                      contains="application_id")
        (state / "state.sqlite3").unlink()
        self.env.run()
        conn = sqlite3.connect(state / "state.sqlite3")
        conn.execute("PRAGMA user_version=7")
        conn.close()
        self.env.fail("status", contains="版本")

    def test_refuses_implicit_rebuild(self):
        self.env.run()
        (self.env.root / ".orchestration" / "state.sqlite3").unlink()
        self.env.fail("create-run", {"run_id": "r2", "objective": "x", "loaded_agents": {"observed_from": "t", "names": []}},
                      contains="拒绝隐式重建")

    def test_dependencies_must_be_accepted(self):
        self.env.run()
        self.env.add("a", write=["src/a.py"])
        self.env.add("b", write=["src/b.py"], deps=["a"])
        self.env.fail("start", {"run_id": "r1", "task_id": "b"}, contains="尚未 accepted")
        self.env.complete("a", files=["src/a.py"])
        self.env.start("b")

    def test_unknown_dependency_and_cycle(self):
        self.env.run()
        self.env.fail("add-task", self.env.contract("a", deps=["zzz"]), contains="未登记")
        batch = {"tasks": [self.env.contract("a", deps=["b"]), self.env.contract("b", deps=["a"])]}
        self.env.fail("add-task", batch, contains="环")

    def test_batch_add_is_atomic(self):
        self.env.run()
        batch = {"tasks": [self.env.contract("ok", write=["src/a.py"]), self.env.contract("bad", write=[".git/config"])]}
        self.env.fail("add-task", batch, contains="受保护")
        status = self.env.call("status", extra=["--run-id", "r1"])
        self.assertEqual(status["tasks"], [])
        batch = {"tasks": [self.env.contract("a", write=["src/a.py"]), self.env.contract("b", deps=["a"], write=["src/b.py"])]}
        self.assertEqual(len(self.env.call("add-task", batch)["tasks"]), 2)

    def test_contract_fields_added(self):
        self.env.run()
        self.env.add("t1", write=["src/a.py"], read_scope=["docs"])
        status = self.env.call("status", extra=["--run-id", "r1"])
        contract = status["tasks"][0]["contract"]
        self.assertEqual(contract["read_scope"], ["docs", "src/a.py"])
        self.assertEqual(contract["read_scope_added_from_write"], ["src/a.py"])
        self.assertTrue(contract["baseline_calls"])
        self.assertEqual(contract["project_rules"], [])
        self.assertEqual(contract["max_attempts"], 2)

    def test_rejects_unknown_contract_fields_and_bad_attempts(self):
        self.env.run()
        self.env.fail("add-task", self.env.contract("t1", extra_field=1), contains="未知字段")
        self.env.fail("add-task", self.env.contract("t1", max_attempts=3), contains="max_attempts")
        self.env.fail("add-task", self.env.contract("t1", max_attempts=True), contains="max_attempts")
        self.env.fail("add-task", self.env.contract("t1", acceptance=[]), contains="acceptance")


class WriteProtection(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_default_and_configured_protected_paths(self):
        self.env.write(".codex/orchestration.json", json.dumps({"schema": orchlib.CONFIG_SCHEMA, "protected_paths": ["data/raw"]}))
        self.env.run()
        for bad in (".git/config", ".codex/agents/x.toml", ".agents/skills/x", ".env",
                    ".orchestration/state.sqlite3-wal", ".orchestration", "data/raw/a.csv", "data", ".", "../outside.txt"):
            with self.subTest(path=bad):
                self.env.fail("add-task", self.env.contract("t", write=[bad]))
        self.env.add("ok", write=["data/clean/a.csv"])

    def test_read_only_roles_cannot_write(self):
        self.env.run()
        for role in ("reviewer", "researcher"):
            with self.subTest(role=role):
                self.env.fail("add-task", self.env.contract(f"t-{role}", role=role, write=["src/a.py"]), contains="只读")

    def test_parent_child_conflict_held_until_release(self):
        self.env.run()
        self.env.add("parent", write=["src"])
        self.env.add("child", write=["src/pkg/a.py"])
        self.env.start("parent")
        self.env.bind("parent")
        self.env.fail("start", {"run_id": "r1", "task_id": "child"}, contains="重叠")
        self.env.call("interrupt", {"run_id": "r1", "task_id": "parent", "reason": "用户中止"})
        self.env.fail("start", {"run_id": "r1", "task_id": "child"}, contains="重叠")
        self.env.release("parent")
        self.env.start("child")


class Attempts(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.env.run()

    def test_two_attempt_limit_and_retry_rules(self):
        self.env.add("t", write=["src/a.py"])
        self.env.start("t")
        self.env.bind("t")
        self.env.call("fail", {"run_id": "r1", "task_id": "t", "reason": "测试失败"})
        self.env.fail("retry", {"run_id": "r1", "task_id": "t", "reason": "重试"}, contains="release")
        self.env.release("t")
        self.env.call("retry", {"run_id": "r1", "task_id": "t", "reason": "修正输入后重试"})
        second = self.env.start("t")
        self.assertEqual(second["attempt"], 2)
        self.assertTrue(second["contract_path"].endswith("t-attempt-2.json"))
        self.env.bind("t")
        self.env.call("fail", {"run_id": "r1", "task_id": "t", "reason": "再次失败"})
        self.env.release("t")
        self.env.fail("retry", {"run_id": "r1", "task_id": "t", "reason": "第三次"}, contains="尝试")

    def test_single_attempt_contract(self):
        self.env.add("t", write=["src/a.py"], max_attempts=1)
        self.env.start("t")
        self.env.bind("t")
        self.env.call("fail", {"run_id": "r1", "task_id": "t", "reason": "失败"})
        self.env.release("t")
        self.env.fail("retry", {"run_id": "r1", "task_id": "t", "reason": "x"}, contains="尝试")

    def test_cancel_start_not_counted(self):
        self.env.add("t", write=["src/a.py"], max_attempts=1)
        self.env.start("t")
        self.env.call("cancel-start", {"run_id": "r1", "task_id": "t", "reason": "派发被拒"})
        again = self.env.start("t")
        self.assertEqual(again["attempt"], 2)
        brief = self.env.call("status", extra=["--run-id", "r1", "--brief"])
        self.assertEqual(brief["tasks"][0]["attempts"], 1)

    def test_bind_once_and_spawn_checks(self):
        self.env.add("t", write=["src/a.py"])
        self.env.start("t")
        self.assertFalse(self.env.bind("t", inherits_context=True)["ok"])
        self.assertFalse(self.env.bind("t", model_override="m")["ok"])
        self.assertFalse(self.env.bind("t", reasoning_effort_override="high")["ok"])
        self.assertFalse(self.env.bind("t", agent_type="orch_worker")["ok"])
        self.assertFalse(self.env.bind("t", tool_version="v3")["ok"])
        self.assertTrue(self.env.bind("t")["ok"])
        self.assertFalse(self.env.bind("t")["ok"])
        self.env.fail("cancel-start", {"run_id": "r1", "task_id": "t", "reason": "x"})

    def test_contract_export_exclusive(self):
        self.env.add("t", write=["src/a.py"])
        path = self.env.root / ".orchestration" / "contracts" / "r1" / "t-attempt-1.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        self.env.fail("start", {"run_id": "r1", "task_id": "t"}, contains="拒绝覆盖")
        path.unlink()
        first = self.env.start("t")
        data = path.read_bytes()
        self.assertEqual(sha(data), first["contract_sha256"])
        exported = json.loads(data)
        self.assertEqual(exported["attempt"], 1)
        self.assertEqual(data.decode("utf-8"), json.dumps(exported, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        conn = sqlite3.connect(self.env.root / ".orchestration" / "state.sqlite3")
        conn.execute("DELETE FROM attempts")
        conn.execute("UPDATE tasks SET status='planned'")
        conn.commit()
        conn.close()
        again = self.env.start("t")
        self.assertEqual(again["contract_sha256"], first["contract_sha256"])

    def test_message_contains_only_reference(self):
        self.env.add("t", write=["src/a.py"])
        started = self.env.start("t")
        self.assertIn(started["contract_path"], started["message"])
        self.assertIn(started["contract_sha256"], started["message"])
        self.assertNotIn("完成 t", started["message"])


class Concurrency(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_limit_sources(self):
        self.assertEqual(self.env.call("config")["concurrency"]["effective"], 6)
        (self.env.home / "config.toml").write_text("[agents]\nmax_concurrent_threads_per_session = 5\n", encoding="utf-8")
        self.assertEqual(self.env.call("config")["concurrency"]["effective"], 5)
        self.env.write(".codex/config.toml", "[agents]\nmax_concurrent_threads_per_session = 3\n")
        self.assertEqual(self.env.call("config")["concurrency"]["effective"], 3)
        self.env.write(".codex/orchestration.json", json.dumps({"schema": orchlib.CONFIG_SCHEMA, "max_live": 2}))
        self.assertEqual(self.env.call("config")["concurrency"]["effective"], 2)
        self.env.write(".codex/orchestration.json", json.dumps({"schema": orchlib.CONFIG_SCHEMA, "max_live": 9}))
        self.assertEqual(self.env.call("config")["concurrency"]["effective"], 3)

    def test_runs_share_slots_and_write_ownership(self):
        self.env.write(".codex/orchestration.json", json.dumps({"schema": orchlib.CONFIG_SCHEMA, "max_live": 2}))
        self.env.run("r1")
        self.env.run("r2")
        self.env.add("a", run_id="r1", write=["src/a.py"])
        self.env.add("b", run_id="r2", write=["src/a.py"])
        self.env.add("c", run_id="r2", write=["src/c.py"])
        self.env.add("d", run_id="r2", role="researcher")
        self.env.start("a", "r1")
        self.env.fail("start", {"run_id": "r2", "task_id": "b"}, contains="重叠")
        self.env.start("c", "r2")
        self.env.fail("start", {"run_id": "r2", "task_id": "d"}, contains="名额")


class Reports(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.env.run()

    def started(self, task_id="t", role="worker", write=("src/a.py",)):
        self.env.add(task_id, role=role, write=list(write))
        started = self.env.start(task_id)
        self.env.bind(task_id)
        return started

    def check(self, delivery, task_id="t"):
        return self.env.call("check-report", {"run_id": "r1", "task_id": task_id, "delivery": delivery}, ok=None)

    def test_contradictions(self):
        started = self.started()
        self.env.write("src/a.py", "x")
        good = self.env.delivery(started, artifacts=["src/a.py"])
        self.assertTrue(self.check(good)["ok"])
        cases = {
            "unresolved 为空": dict(checks=[{"name": "c", "passed": False, "evidence": "e"}]),
            "前后矛盾": dict(unresolved=["还有问题"]),
            "只有 reviewer": dict(findings=[{"location": "a", "trigger": "b", "impact": "c", "evidence": "d", "blocking": True}]),
            "operations": dict(operations=[]),
            "checks 必须": dict(checks=[]),
            "exit_code": dict(operations=[{"action": "a", "exit_code": "0", "result": "r"}]),
            "布尔": dict(checks=[{"name": "c", "passed": "yes", "evidence": "e"}]),
        }
        for needle, change in cases.items():
            with self.subTest(needle=needle):
                body = self.env.delivery(started, artifacts=["src/a.py"])
                body.update(change)
                result = self.check(body)
                self.assertFalse(result["ok"])
                self.assertIn(needle, "；".join(result["problems"]))

    def test_receipt_checks(self):
        started = self.started()
        body = self.env.delivery(started)
        del body["contract_receipt"]
        self.assertIn("contract_receipt", "；".join(self.check(body)["problems"]))
        body = self.env.delivery(started)
        body["contract_receipt"]["sha256"] = "0" * 64
        self.assertFalse(self.check(body)["ok"])
        body = self.env.delivery(started)
        body["contract_receipt"]["inputs_verified"] = False
        self.assertFalse(self.check(body)["ok"])
        body = self.env.delivery(started)
        body["contract_receipt"]["path"] = str(self.env.root / "other.json")
        self.assertFalse(self.check(body)["ok"])

    def test_read_only_artifacts_and_reviewer_findings_required(self):
        started = self.started("rv", role="reviewer", write=())
        body = self.env.delivery(started, findings=[])
        self.assertTrue(self.check(body, "rv")["ok"])
        del body["findings"]
        self.assertFalse(self.check(body, "rv")["ok"])
        self.env.write("src/a.py", "x")
        body = self.env.delivery(started, artifacts=["src/a.py"], findings=[])
        self.assertIn("只读", "；".join(self.check(body, "rv")["problems"]))

    def test_check_report_does_not_change_state(self):
        started = self.started()
        self.check({"bad": True})
        self.env.write("src/a.py", "x")
        self.env.report("t", self.env.delivery(started, artifacts=["src/a.py"]))

    def test_correction_once_then_fail(self):
        started = self.started()
        bad = self.env.delivery(started, unresolved=["x"])
        first = self.env.report("t", bad, ok=False)
        self.assertIn("更正", first["next"])
        self.assertTrue(Path(first["saved"]["reply"]).is_file())
        second = self.env.report("t", bad, ok=False)
        self.assertEqual(second["status"], "failed")
        self.assertNotEqual(first["saved"]["reply"], second["saved"]["reply"])
        brief = self.env.call("status", extra=["--run-id", "r1", "--brief"])
        self.assertEqual(brief["tasks"][0]["status"], "failed")
        self.assertEqual(brief["tasks"][0]["attempts"], 1)

    def test_correction_then_success(self):
        started = self.started()
        self.env.report("t", self.env.delivery(started, unresolved=["x"]), ok=False)
        self.env.write("src/a.py", "x")
        self.env.report("t", self.env.delivery(started, artifacts=["src/a.py"]))

    def test_non_json_reply(self):
        self.started()
        result = self.env.call("report", {"run_id": "r1", "task_id": "t", "reply": "完成了！"}, ok=False)
        self.assertIn("JSON", "；".join(result["problems"]))

    def test_fenced_reply_and_reply_path(self):
        started = self.started()
        self.env.write("src/a.py", "x")
        text = "```json\n" + json.dumps(self.env.delivery(started, artifacts=["src/a.py"])) + "\n```"
        reply = self.env.write("notes/reply.txt", text)
        self.env.call("report", {"run_id": "r1", "task_id": "t", "reply_path": str(reply)})

    def test_artifact_scope_and_hash(self):
        started = self.started()
        self.env.write("src/a.py", "x")
        self.env.write("src/other.py", "y")
        body = self.env.delivery(started, artifacts=["src/other.py"])
        self.assertIn("写入范围", "；".join(self.check(body)["problems"]))
        body = self.env.delivery(started, artifacts=["src/a.py"])
        body["artifacts"][0]["sha256"] = "1" * 64
        self.assertIn("哈希", "；".join(self.check(body)["problems"]))

    def test_artifact_change_blocks_accept(self):
        started = self.started()
        self.env.write("src/a.py", "x")
        self.env.report("t", self.env.delivery(started, artifacts=["src/a.py"]))
        self.env.release("t")
        self.env.write("src/a.py", "changed")
        result = self.env.accept("t", ok=False)
        self.assertIn("变化", result["error"])

    def test_accept_requires_release_and_evidence(self):
        started = self.started()
        self.env.write("src/a.py", "x")
        self.env.report("t", self.env.delivery(started, artifacts=["src/a.py"]))
        self.assertIn("release", self.env.accept("t", ok=False)["error"])
        self.env.fail("release", {"run_id": "r1", "task_id": "t", "terminal_confirmed": False, "basis": "x"})
        self.env.release("t")
        bad = {"checks": [{"name": "c", "passed": True, "evidence": "e"}], "evidence": ["notes/missing.txt"], "unresolved": []}
        self.env.fail("accept", {"run_id": "r1", "task_id": "t", "decision": bad}, contains="不存在")
        result = self.env.accept("t")
        record = json.loads(Path(result["decision_file"]).read_text(encoding="utf-8"))
        self.assertEqual(len(record["evidence"][0]["sha256"]), 64)

    def test_inputs_hash_including_outside_path(self):
        outside = self.env.base / "outside.txt"
        outside.write_text("spec", encoding="utf-8")
        good = [{"path": str(outside), "sha256": orchlib.sha256_file(outside)}]
        self.env.fail("add-task", self.env.contract("x", inputs=[{"path": str(outside), "sha256": "0" * 64}]), contains="哈希不符")
        self.env.add("x", inputs=good)
        outside.write_text("changed", encoding="utf-8")
        self.env.fail("start", {"run_id": "r1", "task_id": "x"}, contains="哈希不符")


class ReviewFlow(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.env.run()

    def test_findings_followup_and_finish(self):
        env = self.env
        env.add("impl", write=["src/a.py"])
        env.complete("impl", files=["src/a.py"])
        env.add("review", role="reviewer", deps=["impl"])
        finding = {"location": "src/a.py:1", "trigger": "空输入", "impact": "崩溃", "evidence": "读代码", "blocking": True}
        started = env.start("review")
        env.bind("review")
        env.report("review", env.delivery(started, findings=[finding]))
        env.release("review")
        missing = [{"index": 0, "disposition": "confirmed", "evidence": "复现"}]
        self.assertIn("followup_task_id", env.accept("review", findings=missing, ok=False)["error"])
        unknown = [{"index": 0, "disposition": "confirmed", "evidence": "复现", "followup_task_id": "nope"}]
        env.accept("review", findings=unknown, ok=False)
        self.assertIn("逐条", env.accept("review", findings=[], ok=False)["error"])
        env.add("fix", role="engineer", write=["src/a.py"], deps=["review"])
        good = [{"index": 0, "disposition": "confirmed", "evidence": "复现", "followup_task_id": "fix"}]
        env.accept("review", findings=good)
        env.fail("finish", {"run_id": "r1"}, contains="fix")
        env.complete("fix", files=["src/a.py"])
        env.add("rereview", role="reviewer", deps=["fix"])
        env.complete("rereview", findings=[], dispositions=[])
        report = env.write("notes/final.md", "已验证：全部")
        result = env.call("finish", {"run_id": "r1", "report": str(report)})
        self.assertTrue(Path(result["summary_file"]).is_file())
        env.fail("add-task", env.contract("late"), contains="已结束")

    def test_rejected_finding_needs_no_followup(self):
        env = self.env
        env.add("review", role="reviewer")
        finding = {"location": "x", "trigger": "y", "impact": "z", "evidence": "w", "blocking": True}
        env.complete("review", findings=[finding], dispositions=[{"index": 0, "disposition": "rejected", "evidence": "不成立"}])

    def test_close_run_keeps_failures(self):
        env = self.env
        env.add("t", write=["src/a.py"], max_attempts=1)
        env.start("t")
        env.bind("t")
        env.call("fail", {"run_id": "r1", "task_id": "t", "reason": "不可修复"})
        env.fail("close-run", {"run_id": "r1", "reason": "失败", "evidence": [env.evidence()]}, contains="release")
        env.release("t")
        env.call("close-run", {"run_id": "r1", "reason": "尝试用尽", "evidence": [env.evidence()]})
        status = env.call("status", extra=["--run-id", "r1"])
        self.assertEqual(status["status"], "closed_with_failures")
        self.assertEqual(status["tasks"][0]["attempt_records"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

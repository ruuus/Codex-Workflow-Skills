import contextlib
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "multi-agent-orchestration" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True

import journal  # noqa: E402
import orchlib as ol  # noqa: E402
import roles  # noqa: E402

CATALOG = {
    "models": [
        {"slug": "example-model-a", "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "max", "ultra")]},
        {"slug": "example-model-b", "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "xhigh")]},
        {"slug": "example-model-c", "supported_reasoning_levels": [{"effort": e} for e in ("low", "turbo")]},
    ]
}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "proj"
        self.root.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    def cli(self, module, args, ok=True):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = module.main(args)
        result = json.loads(buf.getvalue())
        if ok is True:
            self.assertEqual(code, 0, result)
        elif ok is False:
            self.assertNotEqual(code, 0, result)
        return result

    def roles(self, *args, ok=True):
        return self.cli(roles, list(args), ok)

    def journal(self, command, payload=None, ok=True):
        args = ["--root", str(self.root), command]
        if payload is not None:
            self.n += 1
            path = self.base / f"in-{self.n}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            args += ["--input", str(path)]
        return self.cli(journal, args, ok)

    def write_config(self, data):
        path = self.root / ".codex" / "orchestration.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data)
        path.write_text(text, encoding="utf-8")
        return path

    def write_catalog(self, where="home", catalog=CATALOG, rel="catalog/models.json"):
        folder = self.home if where == "home" else self.root / ".codex"
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(json.dumps(catalog), encoding="utf-8")
        config = folder / "config.toml"
        config.write_text(f'model_catalog_json = "{rel}"\n', encoding="utf-8")
        return folder / rel

    def config(self, roles_cfg=None, **extra):
        data = {"schema": ol.CONFIG_SCHEMA}
        if roles_cfg is not None:
            data["roles"] = roles_cfg
        data.update(extra)
        return data

    def validate(self, data):
        return ol.validate_project_config(self.root, data)


class Rendering(Base):
    def test_render_is_deterministic_and_parses(self):
        for role in ol.ROLES:
            text = ol.render_global_agent(role)
            self.assertEqual(text, ol.render_global_agent(role))
            self.assertTrue(text.startswith(ol.GENERATED_MARKER + "\n"))
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["name"], f"orch_{role}")
            self.assertNotIn("model", parsed)
            self.assertNotIn("model_reasoning_effort", parsed)
            self.assertEqual(parsed.get("sandbox_mode") == "read-only", role in ol.READ_ONLY_ROLES)
            self.assertIn("Common subagent rules", parsed["developer_instructions"])
            self.assertNotIn("\r", text)

    def test_rejects_triple_quote(self):
        with self.assertRaises(ol.OrchError):
            ol.render_agent_toml({"name": "x", "description": "d", "developer_instructions": "a ''' b"})

    def test_escapes_basic_strings(self):
        text = ol.render_agent_toml({"name": "x", "description": 'say "hi" \\ ok', "developer_instructions": "body"})
        self.assertEqual(tomllib.loads(text)["description"], 'say "hi" \\ ok')


class InstallGlobal(Base):
    def agent(self, role="worker"):
        return self.home / "agents" / f"orch_{role}.toml"

    def test_create_skip_update(self):
        first = self.roles("install-global")
        self.assertEqual({a["action"] for a in first["agents"]}, {"created"})
        second = self.roles("install-global")
        self.assertEqual({a["action"] for a in second["agents"]}, {"skipped"})
        old = ol.render_agent_toml({"name": "orch_worker", "description": "旧版", "developer_instructions": "旧指令"})
        self.agent().write_text(old, encoding="utf-8", newline="\n")
        self.assertEqual(self.roles("doctor")["global_agents"]["orch_worker"], "outdated")
        third = self.roles("install-global")
        actions = {a["agent"]: a["action"] for a in third["agents"]}
        self.assertEqual(actions["orch_worker"], "updated")
        self.assertEqual(self.agent().read_text(encoding="utf-8"), ol.render_global_agent("worker"))

    def test_refuses_modified_and_foreign_then_replace_backs_up(self):
        self.roles("install-global")
        modified = self.agent().read_text(encoding="utf-8") + "# 手改\n"
        self.agent().write_text(modified, encoding="utf-8", newline="\n")
        self.agent("editor").write_text('name = "orch_editor"\n', encoding="utf-8")
        doctor = self.roles("doctor")["global_agents"]
        self.assertEqual(doctor["orch_worker"], "modified")
        self.assertEqual(doctor["orch_editor"], "foreign")
        result = self.roles("install-global", ok=False)
        self.assertIn("--replace", result["error"])
        self.assertEqual(self.agent().read_text(encoding="utf-8"), modified)
        replaced = self.roles("install-global", "--replace")
        backup = Path(replaced["backup"])
        self.assertTrue(str(backup).startswith(str(self.home / "backups" / "multi-agent-orchestration")))
        self.assertEqual((backup / "orch_worker.toml").read_text(encoding="utf-8"), modified)
        self.assertTrue((backup / "orch_editor.toml").is_file())
        self.assertEqual(set(self.roles("doctor")["global_agents"].values()), {"current"})

    def test_doctor_missing(self):
        self.assertEqual(set(self.roles("doctor")["global_agents"].values()), {"missing"})


class ConfigValidation(Base):
    def test_schema_required(self):
        with self.assertRaisesRegex(ol.OrchError, "项目自带"):
            self.validate({"roles": {}})
        with self.assertRaisesRegex(ol.OrchError, "schema"):
            self.validate({"schema": "other/v1"})

    def test_unknown_and_duplicate_keys(self):
        with self.assertRaisesRegex(ol.OrchError, "未知键"):
            self.validate(self.config(extra=1))
        self.write_config('{"schema": "multi-agent-orchestration/v1", "rules": [], "rules": []}')
        with self.assertRaisesRegex(ol.OrchError, "重复"):
            ol.load_project_config(self.root)
        with self.assertRaisesRegex(ol.OrchError, "未知键"):
            self.validate(self.config({"worker": {"candidates": [{"agent": "orch_worker", "priority": 1, "x": 1}]}}))

    def test_priority_rules(self):
        for bad in (True, -1, 11, 1.0, "1"):
            with self.subTest(priority=bad), self.assertRaisesRegex(ol.OrchError, "priority"):
                self.validate(self.config({"worker": {"candidates": [{"agent": "orch_worker", "priority": bad}]}}))
        with self.assertRaisesRegex(ol.OrchError, "重复"):
            self.validate(self.config({"worker": {"candidates": [
                {"agent": "orch_worker", "priority": 1}, {"agent": "orch_worker", "priority": 2}]}}))

    def test_managed_naming_and_fields(self):
        ok = self.config({"worker": {"candidates": [
            {"agent": "proj_worker", "priority": 1, "model": "m", "reasoning_effort": "high", "instructions": "x"},
            {"agent": "proj_worker_fast_v2", "priority": 2},
        ]}})
        cfg = self.validate(ok)
        self.assertTrue(cfg["roles"]["worker"][0]["managed"])
        with self.assertRaisesRegex(ol.OrchError, "proj_worker"):
            self.validate(self.config({"worker": {"candidates": [{"agent": "proj_reviewer", "priority": 1}]}}))
        with self.assertRaisesRegex(ol.OrchError, "引用候选"):
            self.validate(self.config({"worker": {"candidates": [{"agent": "orch_worker", "priority": 1, "model": "m"}]}}))
        with self.assertRaisesRegex(ol.OrchError, "同时给出"):
            self.validate(self.config({"worker": {"candidates": [{"agent": "proj_worker", "priority": 1, "reasoning_effort": "high"}]}}))
        with self.assertRaisesRegex(ol.OrchError, "未知"):
            self.validate(self.config({"worker": {"candidates": [{"agent": "proj_worker", "priority": 1, "model": "m", "reasoning_effort": "huge"}]}}))
        custom = self.validate(self.config({"worker": {"candidates": [{"agent": "team_worker", "priority": 1}]}}, agent_prefix="team"))
        self.assertTrue(custom["roles"]["worker"][0]["managed"])
        with self.assertRaisesRegex(ol.OrchError, "agent_prefix"):
            self.validate(self.config(agent_prefix="orch"))

    def test_dispatch_rules(self):
        self.assertEqual(self.validate(self.config())["dispatch"], "auto")
        self.assertEqual(self.validate(self.config({}))["dispatch"], "named")
        with self.assertRaisesRegex(ol.OrchError, "named"):
            self.validate(self.config({}, dispatch="auto"))
        with self.assertRaisesRegex(ol.OrchError, "dispatch"):
            self.validate(self.config(dispatch="fast"))

    def test_paths_and_limits(self):
        with self.assertRaisesRegex(ol.OrchError, "项目内"):
            self.validate(self.config(state_dir="../state"))
        with self.assertRaisesRegex(ol.OrchError, "项目根"):
            self.validate(self.config(state_dir="."))
        with self.assertRaisesRegex(ol.OrchError, "max_live"):
            self.validate(self.config(max_live=True))
        with self.assertRaisesRegex(ol.OrchError, "preflight"):
            self.validate(self.config(startup={"preflight": {"command": "x"}}))

    def test_journal_rejects_foreign_config(self):
        self.write_config({"version": 1, "roles": {}})
        result = self.journal("config", ok=False)
        self.assertIn("项目自带", result["error"])


class Catalog(Base):
    def test_max_available(self):
        self.assertEqual(ol.max_available_effort(["low", "max", "ultra"]), "max")
        self.assertEqual(ol.max_available_effort(["low", "xhigh", "high"]), "xhigh")
        self.assertEqual(ol.max_available_effort(["minimal", "none"]), "minimal")
        with self.assertRaisesRegex(ol.OrchError, "未知"):
            ol.max_available_effort(["low", "turbo"])

    def test_catalog_path_resolution(self):
        self.assertIsNone(ol.catalog_location(self.root))
        home_catalog = self.write_catalog("home")
        self.assertEqual(ol.catalog_location(self.root)["path"], home_catalog)
        project_catalog = self.write_catalog("project", rel="m.json")
        self.assertEqual(ol.catalog_location(self.root)["path"], project_catalog)

    def test_resolve_model(self):
        self.assertFalse(ol.resolve_model(None, "anything", "high")["catalog_verified"])
        with self.assertRaisesRegex(ol.OrchError, "max-available"):
            ol.resolve_model(None, "anything", "max-available")
        self.write_catalog()
        catalog = ol.load_catalog(self.root)
        self.assertEqual(ol.resolve_model(catalog, "example-model-a", "max-available")["reasoning_effort"], "max")
        self.assertEqual(ol.resolve_model(catalog, "example-model-b", "max-available")["reasoning_effort"], "xhigh")
        with self.assertRaisesRegex(ol.OrchError, "未知"):
            ol.resolve_model(catalog, "example-model-c", "max-available")
        with self.assertRaisesRegex(ol.OrchError, "不支持"):
            ol.resolve_model(catalog, "example-model-b", "max")
        with self.assertRaisesRegex(ol.OrchError, "没有模型"):
            ol.resolve_model(catalog, "missing", None)


class Generate(Base):
    def setup_project(self):
        self.write_catalog()
        self.write_config(self.config({
            "worker": {"candidates": [
                {"agent": "proj_worker", "priority": 1, "model": "example-model-a", "reasoning_effort": "max-available"},
                {"agent": "orch_worker", "priority": 2},
            ]},
            "reviewer": {"candidates": [{"agent": "proj_reviewer", "priority": 1, "instructions": "重点看边界条件。"}]},
        }, rules=["不要修改 data/raw"]))

    def agent(self, name):
        return self.root / ".codex" / "agents" / f"{name}.toml"

    def test_generate_content(self):
        self.setup_project()
        result = self.roles("generate", "--root", str(self.root))
        self.assertEqual({a["action"] for a in result["agents"]}, {"created"})
        worker = tomllib.loads(self.agent("proj_worker").read_text(encoding="utf-8"))
        self.assertEqual(worker["model"], "example-model-a")
        self.assertEqual(worker["model_reasoning_effort"], "max")
        self.assertIn("不要修改 data/raw", worker["developer_instructions"])
        reviewer = tomllib.loads(self.agent("proj_reviewer").read_text(encoding="utf-8"))
        self.assertEqual(reviewer["sandbox_mode"], "read-only")
        self.assertNotIn("model", reviewer)
        self.assertIn("重点看边界条件。", reviewer["developer_instructions"])
        doctor = self.roles("doctor", "--root", str(self.root))
        self.assertEqual(doctor["project"]["managed_agents"]["proj_worker"]["status"], "current")

    def test_generate_overwrites_only_unmodified(self):
        self.setup_project()
        self.roles("generate", "--root", str(self.root))
        self.assertEqual({a["action"] for a in self.roles("generate", "--root", str(self.root))["agents"]}, {"skipped"})
        hand = self.agent("proj_reviewer").read_text(encoding="utf-8").replace("重点看边界条件。", "手改")
        self.agent("proj_reviewer").write_text(hand, encoding="utf-8", newline="\n")
        config = json.loads((self.root / ".codex" / "orchestration.json").read_text(encoding="utf-8"))
        config["rules"] = ["新规则"]
        self.write_config(config)
        result = self.roles("generate", "--root", str(self.root), ok=False)
        actions = {a["agent"]: a["action"] for a in result["agents"]}
        self.assertEqual(actions, {"proj_worker": "updated", "proj_reviewer": "refused"})
        self.assertEqual(self.agent("proj_reviewer").read_text(encoding="utf-8"), hand)

    def test_removed_managed_files_are_reported(self):
        self.setup_project()
        self.roles("generate", "--root", str(self.root))
        config = json.loads((self.root / ".codex" / "orchestration.json").read_text(encoding="utf-8"))
        del config["roles"]["reviewer"]
        self.write_config(config)
        result = self.roles("generate", "--root", str(self.root))
        self.assertEqual([Path(p).name for p in result["removed_from_config"]], ["proj_reviewer.toml"])
        self.assertTrue(self.agent("proj_reviewer").exists())

    def test_generate_without_catalog(self):
        self.write_config(self.config({"worker": {"candidates": [{"agent": "proj_worker", "priority": 1, "model": "x", "reasoning_effort": "high"}]}}))
        result = self.roles("generate", "--root", str(self.root))
        self.assertFalse(result["agents"][0]["catalog_verified"])
        self.write_config(self.config({"worker": {"candidates": [{"agent": "proj_worker", "priority": 1, "model": "x", "reasoning_effort": "max-available"}]}}))
        self.roles("generate", "--root", str(self.root), ok=False)


class Selection(Base):
    def select(self, role, loaded, requested=None):
        return ol.select_dispatch(self.root, ol.load_project_config(self.root), role, loaded, requested)

    def test_no_config_named_inline_stop(self):
        self.assertEqual(self.select("worker", ["orch_worker"])["mode"], "named")
        inline = self.select("worker", [])
        self.assertEqual(inline["mode"], "inline")
        self.assertIsNone(inline["agent_type"])
        self.assertIn("orch_worker", inline["degraded_reason"])
        self.write_config(self.config(dispatch="named"))
        with self.assertRaisesRegex(ol.OrchError, "dispatch=named"):
            self.select("worker", [])

    def test_priority_loaded_and_gaps(self):
        self.write_config(self.config({
            "worker": {"candidates": [
                {"agent": "orch_worker", "priority": 2},
                {"agent": "hand_worker", "priority": 1},
                {"agent": "old_worker", "priority": 0},
            ]},
            "editor": {"candidates": [{"agent": "orch_editor", "priority": 0}]},
        }))
        self.assertEqual(self.select("worker", ["orch_worker", "hand_worker"])["agent_type"], "hand_worker")
        self.assertEqual(self.select("worker", ["orch_worker", "old_worker"])["agent_type"], "orch_worker")
        self.assertEqual(self.select("worker", ["orch_worker", "hand_worker"], "orch_worker")["agent_type"], "orch_worker")
        with self.assertRaisesRegex(ol.OrchError, "priority 0"):
            self.select("worker", ["old_worker"], "old_worker")
        with self.assertRaisesRegex(ol.OrchError, "不降级"):
            self.select("worker", ["old_worker"])
        with self.assertRaisesRegex(ol.OrchError, "停用"):
            self.select("editor", ["orch_editor"])
        with self.assertRaisesRegex(ol.OrchError, "不回退"):
            self.select("reviewer", ["orch_reviewer"])

    def test_managed_requires_current_file(self):
        self.write_config(self.config({"worker": {"candidates": [{"agent": "proj_worker", "priority": 1}]}}))
        with self.assertRaisesRegex(ol.OrchError, "generate"):
            self.select("worker", ["proj_worker"])
        self.roles("generate", "--root", str(self.root))
        dispatch = self.select("worker", ["proj_worker"])
        self.assertEqual(dispatch["source"], "project-managed")
        self.assertEqual(len(dispatch["agent_file_sha256"]), 64)


class Drift(Base):
    def setUp(self):
        super().setUp()
        self.write_catalog()
        self.write_config(self.config({"worker": {"candidates": [
            {"agent": "proj_worker", "priority": 1, "model": "example-model-a", "reasoning_effort": "max-available"}]}},
            rules=["规则一"]))
        self.roles("generate", "--root", str(self.root))
        self.journal("create-run", {"run_id": "r1", "objective": "x", "loaded_agents": {"observed_from": "t", "names": ["proj_worker"]}})

    def add(self, task_id):
        return self.journal("add-task", {
            "run_id": "r1", "task_id": task_id, "role": "worker", "objective": "x", "inputs": [], "read_scope": [],
            "write_scope": [f"src/{task_id}.py"], "dependencies": [], "acceptance": ["a"], "failure_conditions": [],
        })

    def start(self, task_id, ok=True):
        return self.journal("start", {"run_id": "r1", "task_id": task_id}, ok=ok)

    def test_dispatch_recorded(self):
        dispatch = self.add("t")["tasks"][0]["dispatch"]
        self.assertEqual(dispatch["agent_type"], "proj_worker")
        self.assertEqual(dispatch["declared_effort"], "max")
        self.assertEqual(len(dispatch["catalog_entry_sha256"]), 64)
        contract = json.loads(Path(self.start("t")["contract_path"]).read_text(encoding="utf-8"))
        self.assertEqual(contract["project_rules"], ["规则一"])
        self.assertNotIn("role_instructions", contract)

    def test_catalog_entry_change_blocks_start(self):
        self.add("t")
        changed = json.loads(json.dumps(CATALOG))
        changed["models"][0]["context_window"] = 1000
        (self.home / "catalog" / "models.json").write_text(json.dumps(changed), encoding="utf-8")
        self.assertIn("变化", self.start("t", ok=False)["error"])
        self.journal("rebind", {"run_id": "r1", "task_id": "t"})
        self.start("t")

    def test_agent_file_change_blocks_start(self):
        self.add("t")
        path = self.root / ".codex" / "agents" / "proj_worker.toml"
        path.write_text(path.read_text(encoding="utf-8") + "# x\n", encoding="utf-8", newline="\n")
        self.start("t", ok=False)

    def test_config_change_blocks_start_and_rebind_only_unstarted(self):
        self.add("t")
        self.add("u")
        self.start("u")
        config = json.loads((self.root / ".codex" / "orchestration.json").read_text(encoding="utf-8"))
        config["rules"] = ["规则二"]
        self.write_config(config)
        self.roles("generate", "--root", str(self.root))
        self.start("t", ok=False)
        self.journal("rebind", {"run_id": "r1", "task_id": "u"}, ok=False)
        result = self.journal("rebind", {"run_id": "r1", "task_id": "t", "reason": "规则更新"})
        self.assertNotEqual(result["before"]["config_sha256"], result["dispatch"]["config_sha256"])
        contract = json.loads(Path(self.start("t")["contract_path"]).read_text(encoding="utf-8"))
        self.assertEqual(contract["project_rules"], ["规则二"])

    def test_inline_rebind_after_loaded_change(self):
        (self.root / ".codex" / "orchestration.json").unlink()
        self.journal("create-run", {"run_id": "r2", "objective": "x", "loaded_agents": {"observed_from": "t", "names": []}})
        added = self.journal("add-task", {
            "run_id": "r2", "task_id": "t", "role": "researcher", "objective": "x", "inputs": [], "read_scope": [],
            "write_scope": [], "dependencies": [], "acceptance": ["a"], "failure_conditions": [],
        })
        self.assertIn("inline", added["notice"])
        self.journal("set-loaded-agents", {"run_id": "r2", "loaded_agents": {"observed_from": "新会话", "names": ["orch_researcher"]}})
        self.journal("start", {"run_id": "r2", "task_id": "t"}, ok=False)
        rebound = self.journal("rebind", {"run_id": "r2", "task_id": "t"})
        self.assertEqual(rebound["dispatch"]["mode"], "named")
        started = self.journal("start", {"run_id": "r2", "task_id": "t"})
        self.assertEqual(started["dispatch"]["agent_type"], "orch_researcher")


class RoleFocus(Base):
    def task(self, run_id, role="worker", task_id="t"):
        return {
            "run_id": run_id, "task_id": task_id, "role": role, "objective": "x", "inputs": [], "read_scope": [],
            "write_scope": [] if role in ol.READ_ONLY_ROLES else [f"src/{task_id}.py"], "dependencies": [],
            "acceptance": ["a"], "failure_conditions": [],
        }

    def test_validation(self):
        self.assertEqual(self.validate(self.config(role_focus={"reviewer": " 看时点 "}))["role_focus"], {"reviewer": "看时点"})
        for bad in ({"boss": "x"}, {"reviewer": ""}, {"reviewer": 1}, ["x"]):
            with self.assertRaises(ol.OrchError):
                self.validate(self.config(role_focus=bad))

    def test_generated_file_and_contract_carry_focus(self):
        self.write_config(self.config(
            {"worker": {"candidates": [{"agent": "proj_worker", "priority": 1}]}},
            role_focus={"worker": "先写测试", "reviewer": "看时点"}))
        self.roles("generate", "--root", str(self.root))
        text = (self.root / ".codex" / "agents" / "proj_worker.toml").read_text(encoding="utf-8")
        body = tomllib.loads(text)["developer_instructions"]
        self.assertIn("## Project focus for this role\n先写测试", body)
        self.assertNotIn("看时点", body)
        self.journal("create-run", {"run_id": "r1", "objective": "x", "loaded_agents": {"observed_from": "t", "names": ["proj_worker"]}})
        self.journal("add-task", self.task("r1"))
        config = json.loads((self.root / ".codex" / "orchestration.json").read_text(encoding="utf-8"))
        config["role_focus"]["worker"] = "先复现"
        self.write_config(config)
        self.roles("generate", "--root", str(self.root))
        self.journal("start", {"run_id": "r1", "task_id": "t"}, ok=False)
        self.journal("rebind", {"run_id": "r1", "task_id": "t"})
        contract = json.loads(Path(self.journal("start", {"run_id": "r1", "task_id": "t"})["contract_path"]).read_text(encoding="utf-8"))
        self.assertEqual(contract["role_focus"], "先复现")

    def test_global_roles_receive_focus_through_contract(self):
        self.write_config(self.config(role_focus={"researcher": "注明来源日期"}))
        self.journal("create-run", {"run_id": "r1", "objective": "x", "loaded_agents": {"observed_from": "t", "names": []}})
        self.journal("add-task", self.task("r1", role="researcher"))
        self.journal("add-task", self.task("r1", role="worker", task_id="w"))
        researcher = json.loads(Path(self.journal("start", {"run_id": "r1", "task_id": "t"})["contract_path"]).read_text(encoding="utf-8"))
        worker = json.loads(Path(self.journal("start", {"run_id": "r1", "task_id": "w"})["contract_path"]).read_text(encoding="utf-8"))
        self.assertEqual(researcher["role_focus"], "注明来源日期")
        self.assertIn("role_focus", researcher["role_instructions"])
        self.assertNotIn("role_focus", worker)


PRESET = {
    "schema": ol.CONFIG_SCHEMA,
    "rules": ["默认规则"],
    "roles": {
        "worker": {"candidates": [
            {"agent": "proj_worker", "priority": 1, "model": "example-model-a", "reasoning_effort": "max-available"},
            {"agent": "orch_worker", "priority": 2},
        ]},
        "reviewer": {"candidates": [{"agent": "orch_reviewer", "priority": 1}]},
    },
}


class Presets(Base):
    def write_preset(self, name="default", data=PRESET):
        path = self.home / "orchestration" / "presets" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def project_config(self):
        return json.loads((self.root / ".codex" / "orchestration.json").read_text(encoding="utf-8"))

    def test_init_writes_config_with_origin(self):
        path = self.write_preset()
        result = self.roles("init", "--root", str(self.root))
        self.assertEqual(result["enabled_candidates"]["worker"], ["proj_worker", "orch_worker"])
        config = self.project_config()
        self.assertEqual(list(config)[:2], ["schema", "preset"])
        self.assertEqual(config["preset"], {"name": "default", "sha256": ol.sha256_file(path)})
        self.assertEqual(config["roles"], PRESET["roles"])
        self.validate(config)
        self.assertIn("已存在", self.roles("init", "--root", str(self.root), ok=False)["error"])

    def test_init_rejects_missing_or_invalid_preset(self):
        self.assertIn("预设不存在", self.roles("init", "--root", str(self.root), "--preset", "none", ok=False)["error"])
        self.write_preset("bad", dict(PRESET, preset={"name": "x", "sha256": "0" * 64}))
        self.assertIn("preset 键", self.roles("init", "--root", str(self.root), "--preset", "bad", ok=False)["error"])
        self.write_preset("bad2", dict(PRESET, extra=1))
        self.assertIn("未知键", self.roles("init", "--root", str(self.root), "--preset", "bad2", ok=False)["error"])
        self.roles("init", "--root", str(self.root), "--preset", "../x", ok=False)
        self.assertFalse((self.root / ".codex" / "orchestration.json").exists())

    def test_diff_preset(self):
        self.write_preset()
        self.roles("init", "--root", str(self.root))
        clean = self.roles("diff-preset", "--root", str(self.root))
        self.assertEqual((clean["roles"], clean["other_keys_differ"], clean["preset_changed_since_init"]), ({}, [], False))
        newer = json.loads(json.dumps(PRESET))
        newer["roles"]["worker"]["candidates"][0]["priority"] = 0
        newer["roles"]["worker"]["candidates"].append({"agent": "proj_worker_new", "priority": 1, "model": "example-model-b"})
        self.write_preset(data=newer)
        config = self.project_config()
        config["roles"]["reviewer"]["candidates"].append({"agent": "proj_reviewer", "priority": 2})
        config["rules"] = ["项目规则"]
        self.write_config(config)
        diff = self.roles("diff-preset", "--root", str(self.root))
        self.assertTrue(diff["preset_changed_since_init"])
        self.assertEqual(diff["roles"]["worker"]["only_in_preset"], ["proj_worker_new"])
        self.assertEqual(diff["roles"]["worker"]["changed"], {"proj_worker": {"priority": [1, 0]}})
        self.assertEqual(diff["roles"]["reviewer"]["only_in_project"], ["proj_reviewer"])
        self.assertEqual(diff["other_keys_differ"], ["rules"])
        self.assertEqual(self.project_config(), config)

    def test_diff_requires_origin_or_name(self):
        self.write_preset()
        self.write_config(self.config(PRESET["roles"]))
        self.assertIn("--preset", self.roles("diff-preset", "--root", str(self.root), ok=False)["error"])
        diff = self.roles("diff-preset", "--root", str(self.root), "--preset", "default")
        self.assertEqual((diff["preset"]["name"], diff["roles"], diff["recorded_sha256"]), ("default", {}, None))

    def test_doctor_reports_presets(self):
        self.write_preset()
        self.write_preset("broken", {"schema": ol.CONFIG_SCHEMA, "dispatch": "fast"})
        report = self.roles("doctor", ok=False)
        self.assertEqual(report["presets"]["default"], "valid")
        self.assertIn("dispatch", report["presets"]["broken"])


PROFILE = {
    "schema": ol.PROFILE_SCHEMA,
    "models": {
        "example-model-a": {"family": "fam-a", "capabilities": ["instruction-following", "coding"], "suited_roles": ["worker"]},
        "example-model-b": {"family": "fam-a", "suited_roles": ["worker"], "retired": True},
        "example-model-z": {"family": "fam-z"},
    },
}


class ModelProfile(Base):
    def write_profile(self, data=PROFILE):
        path = self.home / "orchestration" / "models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_validation(self):
        self.assertIsNone(ol.load_model_profile())
        self.write_profile()
        self.assertEqual(set(ol.load_model_profile()["models"]), set(PROFILE["models"]))
        bad_entries = ({}, {"family": "f", "speed": 1}, {"family": "f", "suited_roles": ["boss"]},
                       {"family": "f", "verified": "yes"}, {"family": "f", "capabilities": "coding"})
        for entry in bad_entries:
            self.write_profile({"schema": ol.PROFILE_SCHEMA, "models": {"m": entry}})
            with self.assertRaises(ol.OrchError):
                ol.load_model_profile()
        self.write_profile({"schema": "other", "models": {}})
        report = self.roles("doctor", ok=False)
        self.assertIn("error", report["model_profile"])

    def test_catalog_cross_check(self):
        self.write_catalog()
        self.write_profile()
        summary = self.roles("doctor")["model_profile"]
        self.assertEqual(summary["not_in_catalog"], ["example-model-z"])

    def advisories(self, roles_cfg):
        self.write_config(self.config(roles_cfg))
        return self.roles("doctor", "--root", str(self.root), ok=None)["project"]["advisories"]

    def test_advisories(self):
        self.write_profile()
        notes = self.advisories({
            "worker": {"candidates": [
                {"agent": "proj_worker", "priority": 1, "model": "example-model-a"},
                {"agent": "proj_worker_old", "priority": 2, "model": "example-model-b"},
                {"agent": "proj_worker_x", "priority": 0, "model": "example-model-b"},
            ]},
            "reviewer": {"candidates": [
                {"agent": "proj_reviewer", "priority": 1, "model": "example-model-b"},
                {"agent": "proj_reviewer_new", "priority": 2, "model": "example-model-new"},
            ]},
        })
        text = "\n".join(notes)
        self.assertIn("proj_worker_old：模型 example-model-b 在档案中标为 retired", text)
        self.assertNotIn("proj_worker_x", text)
        self.assertIn("proj_reviewer：档案中 example-model-b 的 suited_roles 不含 reviewer", text)
        self.assertIn("proj_reviewer_new：模型 example-model-new 不在模型档案中", text)
        self.assertIn("reviewer 首选 proj_reviewer 与 worker 首选 proj_worker 同属模型家族 fam-a", text)
        self.assertNotIn("proj_worker：", text)

    def test_capabilities_and_inherit(self):
        self.write_profile({"schema": ol.PROFILE_SCHEMA, "models": {"example-model-a": {"family": "f", "capabilities": ["coding"]}}})
        notes = self.advisories({
            "worker": {"candidates": [{"agent": "proj_worker", "priority": 1, "model": "example-model-a"}]},
            "engineer": {"candidates": [{"agent": "orch_engineer", "priority": 1}]},
            "reviewer": {"candidates": [{"agent": "orch_reviewer", "priority": 1}]},
        })
        text = "\n".join(notes)
        self.assertIn("缺少岗位所需能力 instruction-following", text)
        self.assertIn("reviewer 首选 orch_reviewer 与 engineer 首选 orch_engineer 都继承会话模型", text)
        self.assertNotIn("与 worker", text)

    def test_without_profile(self):
        notes = self.advisories({"worker": {"candidates": [{"agent": "orch_worker", "priority": 1}]}})
        self.assertIn("未找到模型档案", notes[0])
        self.write_config(self.config())
        self.assertEqual(self.roles("doctor", "--root", str(self.root))["project"]["advisories"], [])


if __name__ == "__main__":
    unittest.main()

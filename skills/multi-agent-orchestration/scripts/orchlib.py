"""multi-agent-orchestration 的共享工具：配置校验、模型目录、岗位渲染与选择、路径判断。

只依赖 Python 3.11+ 标准库。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

SKILL_NAME = "multi-agent-orchestration"
CONFIG_SCHEMA = "multi-agent-orchestration/v1"
ROLES_SCHEMA = "multi-agent-orchestration/roles/v1"
CONTRACT_SCHEMA = "multi-agent-orchestration/contract/v1"
GENERATED_MARKER = "# generated-by: multi-agent-orchestration/v1"
BODY_HASH_PREFIX = "# body-sha256: "
ROLES = ("worker", "engineer", "reviewer", "researcher", "editor")
READ_ONLY_ROLES = frozenset({"reviewer", "researcher"})
GLOBAL_PREFIX = "orch"
DEFAULT_PREFIX = "proj"
DEFAULT_STATE_DIR = ".orchestration"
DEFAULT_NATIVE_LIMIT = 6
DEFAULT_PROTECTED = (".git", ".codex", ".agents", ".env")
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MAX_AVAILABLE = "max-available"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
VARIANT_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

SKILL_DIR = Path(__file__).resolve().parent.parent
ROLES_FILE = SKILL_DIR / "assets" / "roles.toml"


class OrchError(Exception):
    """面向用户的错误，消息为中文。"""


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def now_local() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def timestamp_slug() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OrchError(f"JSON 中有重复的键：{key}")
        result[key] = value
    return result


def loads_strict(text: str, where: str = "JSON") -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates)
    except json.JSONDecodeError as exc:
        raise OrchError(f"{where} 不是合法 JSON：{exc}") from exc


def read_json_file(path: Path | str, where: str | None = None) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise OrchError(f"无法读取 {path}：{exc}") from exc
    return loads_strict(text, where or str(path))


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


# ---------------------------------------------------------------- 路径


def norm_real(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def path_within(child: str, parent: str) -> bool:
    """两个参数都应先经过 norm_real。"""
    if child == parent:
        return True
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def paths_overlap(first: str, second: str) -> bool:
    return path_within(first, second) or path_within(second, first)


def resolve_in_root(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return Path(os.path.realpath(candidate))


def relative_display(root: Path, path: Path) -> str:
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- 岗位模板


def load_role_templates() -> dict[str, Any]:
    try:
        data = tomllib.loads(ROLES_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OrchError(f"无法解析岗位模板 {ROLES_FILE}：{exc}") from exc
    if data.get("schema") != ROLES_SCHEMA:
        raise OrchError("岗位模板 schema 不符")
    roles = data.get("roles", {})
    if set(roles) != set(ROLES):
        raise OrchError("岗位模板必须且只能定义五个岗位")
    for name in ROLES:
        card = roles[name]
        if set(card) != {"description", "instructions", "read_only"}:
            raise OrchError(f"岗位模板 {name} 字段不符")
        if card["read_only"] != (name in READ_ONLY_ROLES):
            raise OrchError(f"岗位模板 {name} 的 read_only 与约定不符")
    return data


def roles_file_sha256() -> str:
    return sha256_file(ROLES_FILE)


def role_instructions(role: str, templates: dict[str, Any] | None = None) -> str:
    data = templates or load_role_templates()
    body = data["roles"][role]["instructions"].strip()
    common = data["common"].strip()
    return f"{body}\n\n{common}\n"


def role_description(role: str, templates: dict[str, Any] | None = None) -> str:
    data = templates or load_role_templates()
    return data["roles"][role]["description"]


# ---------------------------------------------------------------- TOML 渲染


def toml_basic_string(value: str) -> str:
    out = ['"']
    for char in value:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def toml_multiline_literal(value: str) -> str:
    if "'''" in value:
        raise OrchError("developer_instructions 含有 ''' ，无法用多行字面量字符串表示")
    for char in value:
        code = ord(char)
        if (code < 0x20 and char not in "\n\t") or code == 0x7F:
            raise OrchError("developer_instructions 含有控制字符")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return "'''\n" + text + "'''"


def render_agent_toml(fields: dict[str, str]) -> str:
    """fields 有序：name、description、[model]、[model_reasoning_effort]、[sandbox_mode]、developer_instructions。"""
    lines = []
    for key, value in fields.items():
        if key == "developer_instructions":
            continue
        lines.append(f"{key} = {toml_basic_string(value)}")
    lines.append("developer_instructions = " + toml_multiline_literal(fields["developer_instructions"]))
    body = "\n".join(lines) + "\n"
    header = f"{GENERATED_MARKER}\n{BODY_HASH_PREFIX}{sha256_bytes(body.encode('utf-8'))}\n"
    text = header + body
    parsed = tomllib.loads(text)
    expected = {key: value for key, value in fields.items()}
    expected["developer_instructions"] = toml_multiline_literal(fields["developer_instructions"])[4:-3]
    if parsed != expected:
        raise OrchError("渲染出的岗位 TOML 回读结果与输入不一致")
    return text


def agent_file_status(path: Path, expected_text: str | None) -> str:
    """missing / current / outdated / modified / foreign。"""
    if not path.exists():
        return "missing"
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "foreign"
    lines = text.split("\n", 2)
    if len(lines) < 3 or lines[0] != GENERATED_MARKER or not lines[1].startswith(BODY_HASH_PREFIX):
        return "foreign"
    if sha256_bytes(lines[2].encode("utf-8")) != lines[1][len(BODY_HASH_PREFIX):].strip():
        return "modified"
    if expected_text is not None and text == expected_text:
        return "current"
    return "outdated"


def global_agent_name(role: str) -> str:
    return f"{GLOBAL_PREFIX}_{role}"


def render_global_agent(role: str, templates: dict[str, Any] | None = None) -> str:
    data = templates or load_role_templates()
    fields = {"name": global_agent_name(role), "description": role_description(role, data)}
    if role in READ_ONLY_ROLES:
        fields["sandbox_mode"] = "read-only"
    fields["developer_instructions"] = role_instructions(role, data)
    return render_agent_toml(fields)


# ---------------------------------------------------------------- Codex 配置与模型目录


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OrchError(f"无法解析 {path}：{exc}") from exc


def codex_config_layers(root: Path | None) -> list[tuple[Path, dict[str, Any]]]:
    layers = []
    candidates = []
    if root is not None:
        candidates.append(root / ".codex" / "config.toml")
    candidates.append(codex_home() / "config.toml")
    for path in candidates:
        data = _read_toml(path)
        if data is not None:
            layers.append((path, data))
    return layers


def native_thread_limit(root: Path | None) -> dict[str, Any]:
    for path, data in codex_config_layers(root):
        agents = data.get("agents")
        if isinstance(agents, dict) and "max_concurrent_threads_per_session" in agents:
            value = agents["max_concurrent_threads_per_session"]
            if not is_int(value) or value < 1:
                raise OrchError(f"{path} 中 max_concurrent_threads_per_session 不是正整数")
            return {"value": value, "source": str(path)}
    return {"value": DEFAULT_NATIVE_LIMIT, "source": "default"}


def catalog_location(root: Path | None) -> dict[str, Any] | None:
    for path, data in codex_config_layers(root):
        value = data.get("model_catalog_json")
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise OrchError(f"{path} 中 model_catalog_json 不是字符串")
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = path.parent / target
        return {"path": target, "config": path}
    return None


def load_catalog(root: Path | None) -> dict[str, Any] | None:
    location = catalog_location(root)
    if location is None:
        return None
    data = read_json_file(location["path"], "模型目录")
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise OrchError(f"模型目录 {location['path']} 缺少 models 数组")
    by_slug = {}
    for entry in models:
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str):
            by_slug[entry["slug"]] = entry
    return {"path": location["path"], "config": location["config"], "models": by_slug}


def catalog_efforts(entry: dict[str, Any]) -> list[str]:
    levels = entry.get("supported_reasoning_levels") or []
    result = []
    for level in levels:
        effort = level.get("effort") if isinstance(level, dict) else level
        if isinstance(effort, str):
            result.append(effort)
    return result


def max_available_effort(efforts: list[str]) -> str:
    unknown = [name for name in efforts if name not in EFFORT_ORDER]
    if unknown:
        raise OrchError(f"模型目录含未知推理档位：{', '.join(unknown)}")
    if not efforts:
        raise OrchError("模型目录没有声明任何推理档位")
    if "max" in efforts:
        return "max"
    return max(efforts, key=EFFORT_ORDER.index)


def resolve_model(catalog: dict[str, Any] | None, model: str, effort: str | None) -> dict[str, Any]:
    """返回 {model, reasoning_effort, catalog_verified, catalog_entry_sha256}。"""
    if catalog is None:
        if effort == MAX_AVAILABLE:
            raise OrchError("未配置模型目录（model_catalog_json），不能使用 max-available")
        return {"model": model, "reasoning_effort": effort, "catalog_verified": False, "catalog_entry_sha256": None}
    entry = catalog["models"].get(model)
    if entry is None:
        raise OrchError(f"模型目录中没有模型 {model}")
    efforts = catalog_efforts(entry)
    if effort == MAX_AVAILABLE:
        resolved = max_available_effort(efforts)
    elif effort is not None:
        if effort not in efforts:
            raise OrchError(f"模型 {model} 不支持推理档位 {effort}（目录声明：{', '.join(efforts)}）")
        resolved = effort
    else:
        resolved = None
    return {
        "model": model,
        "reasoning_effort": resolved,
        "catalog_verified": True,
        "catalog_entry_sha256": canonical_sha256(entry),
    }


# ---------------------------------------------------------------- 项目配置


TOP_KEYS = {"schema", "state_dir", "protected_paths", "startup", "rules", "max_live", "dispatch", "agent_prefix", "roles"}
CANDIDATE_KEYS = {"agent", "priority", "model", "reasoning_effort", "instructions"}


def project_config_path(root: Path) -> Path:
    return root / ".codex" / "orchestration.json"


def _check_relative_inside(root: Path, value: Any, label: str, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise OrchError(f"{label} 必须是非空字符串")
    resolved = resolve_in_root(root, value)
    real_root = norm_real(root)
    if not path_within(os.path.normcase(str(resolved)), real_root):
        raise OrchError(f"{label} 必须位于项目内：{value}")
    if not allow_root and os.path.normcase(str(resolved)) == real_root:
        raise OrchError(f"{label} 不能是项目根")
    return value


def managed_name_pattern(prefix: str, role: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}_{role}(?:_[a-z0-9]+(?:_[a-z0-9]+)*)?$")


def validate_project_config(root: Path, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OrchError("项目编排配置必须是 JSON 对象")
    schema = data.get("schema")
    if schema != CONFIG_SCHEMA:
        if schema is None:
            raise OrchError(
                ".codex/orchestration.json 未声明 \"schema\": \"multi-agent-orchestration/v1\"，"
                "视为项目自有的编排配置；请改用项目自带的编排技能"
            )
        raise OrchError(f"不支持的配置 schema：{schema!r}，应为 {CONFIG_SCHEMA}")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise OrchError(f"项目编排配置含未知键：{', '.join(sorted(unknown))}")

    state_dir = data.get("state_dir", DEFAULT_STATE_DIR)
    _check_relative_inside(root, state_dir, "state_dir")

    protected = data.get("protected_paths", [])
    if not isinstance(protected, list):
        raise OrchError("protected_paths 必须是字符串数组")
    for item in protected:
        _check_relative_inside(root, item, "protected_paths 条目", allow_root=False)

    startup = data.get("startup", {})
    if not isinstance(startup, dict) or set(startup) - {"read", "preflight"}:
        raise OrchError("startup 只能包含 read 与 preflight")
    if "read" in startup and not is_str_list(startup["read"]):
        raise OrchError("startup.read 必须是字符串数组")
    if "preflight" in startup:
        preflight = startup["preflight"]
        if not isinstance(preflight, dict) or set(preflight) != {"command", "result"}:
            raise OrchError("startup.preflight 必须且只能包含 command 与 result")
        if not isinstance(preflight["command"], str) or not preflight["command"].strip():
            raise OrchError("startup.preflight.command 必须是非空字符串")
        _check_relative_inside(root, preflight["result"], "startup.preflight.result")

    rules = data.get("rules", [])
    if not is_str_list(rules):
        raise OrchError("rules 必须是非空字符串组成的数组")

    if "max_live" in data:
        if not is_int(data["max_live"]) or data["max_live"] < 1:
            raise OrchError("max_live 必须是正整数")

    prefix = data.get("agent_prefix", DEFAULT_PREFIX)
    if not isinstance(prefix, str) or not PREFIX_RE.match(prefix) or prefix == GLOBAL_PREFIX:
        raise OrchError("agent_prefix 须为小写字母开头的字母数字（最长 16），且不能是 orch")

    has_roles = "roles" in data
    dispatch = data.get("dispatch", "named" if has_roles else "auto")
    if dispatch not in ("auto", "named"):
        raise OrchError("dispatch 只能是 auto 或 named")
    if has_roles and dispatch != "named":
        raise OrchError("配置了 roles 时 dispatch 只能是 named")

    roles_out: dict[str, list[dict[str, Any]]] | None = None
    if has_roles:
        roles = data["roles"]
        if not isinstance(roles, dict):
            raise OrchError("roles 必须是对象")
        roles_out = {}
        for role, card in roles.items():
            if role not in ROLES:
                raise OrchError(f"roles 含未知岗位：{role}")
            if not isinstance(card, dict) or set(card) != {"candidates"}:
                raise OrchError(f"roles.{role} 必须且只能包含 candidates")
            candidates = card["candidates"]
            if not isinstance(candidates, list):
                raise OrchError(f"roles.{role}.candidates 必须是数组")
            seen = set()
            parsed = []
            pattern = managed_name_pattern(prefix, role)
            for index, cand in enumerate(candidates):
                label = f"roles.{role}.candidates[{index}]"
                if not isinstance(cand, dict):
                    raise OrchError(f"{label} 必须是对象")
                extra = set(cand) - CANDIDATE_KEYS
                if extra:
                    raise OrchError(f"{label} 含未知键：{', '.join(sorted(extra))}")
                agent = cand.get("agent")
                if not isinstance(agent, str) or not AGENT_NAME_RE.match(agent):
                    raise OrchError(f"{label}.agent 必须是合法的岗位名称")
                priority = cand.get("priority")
                if not is_int(priority) or not 0 <= priority <= 10:
                    raise OrchError(f"{label}.priority 必须是 0–10 的整数")
                if agent in seen:
                    raise OrchError(f"roles.{role} 中候选 {agent} 重复")
                seen.add(agent)
                managed = agent.startswith(prefix + "_")
                if managed and not pattern.match(agent):
                    raise OrchError(f"{label}.agent 以 {prefix}_ 开头，须命名为 {prefix}_{role} 或 {prefix}_{role}_<变体>")
                if not managed and set(cand) & {"model", "reasoning_effort", "instructions"}:
                    raise OrchError(f"{label} 是引用候选，不能带 model、reasoning_effort 或 instructions")
                if "model" in cand and (not isinstance(cand["model"], str) or not cand["model"]):
                    raise OrchError(f"{label}.model 必须是非空字符串")
                if "reasoning_effort" in cand:
                    effort = cand["reasoning_effort"]
                    if "model" not in cand:
                        raise OrchError(f"{label}.reasoning_effort 必须与 model 同时给出")
                    if effort != MAX_AVAILABLE and effort not in EFFORT_ORDER:
                        raise OrchError(f"{label}.reasoning_effort 未知：{effort!r}")
                if "instructions" in cand and (not isinstance(cand["instructions"], str) or not cand["instructions"].strip()):
                    raise OrchError(f"{label}.instructions 必须是非空字符串")
                parsed.append(dict(cand, managed=managed, order=index))
            roles_out[role] = parsed

    return {
        "schema": CONFIG_SCHEMA,
        "state_dir": state_dir,
        "protected_paths": list(protected),
        "startup": startup,
        "rules": list(rules),
        "max_live": data.get("max_live"),
        "dispatch": dispatch,
        "agent_prefix": prefix,
        "roles": roles_out,
    }


def load_project_config(root: Path) -> dict[str, Any]:
    path = project_config_path(root)
    if not path.exists():
        cfg = validate_project_config(root, {"schema": CONFIG_SCHEMA})
        cfg.update(present=False, path=str(path), sha256=None)
        return cfg
    data = read_json_file(path, ".codex/orchestration.json")
    cfg = validate_project_config(root, data)
    cfg.update(present=True, path=str(path), sha256=sha256_file(path))
    return cfg


# ---------------------------------------------------------------- 项目托管岗位


def project_agents_dir(root: Path) -> Path:
    return root / ".codex" / "agents"


def render_project_agent(
    role: str, cand: dict[str, Any], cfg: dict[str, Any], catalog: dict[str, Any] | None,
    templates: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    data = templates or load_role_templates()
    fields: dict[str, str] = {
        "name": cand["agent"],
        "description": role_description(role, data) + " (project role)",
    }
    resolved = {"model": None, "reasoning_effort": None, "catalog_verified": catalog is not None, "catalog_entry_sha256": None}
    if "model" in cand:
        resolved = resolve_model(catalog, cand["model"], cand.get("reasoning_effort"))
        fields["model"] = resolved["model"]
        if resolved["reasoning_effort"]:
            fields["model_reasoning_effort"] = resolved["reasoning_effort"]
    if role in READ_ONLY_ROLES:
        fields["sandbox_mode"] = "read-only"
    parts = [role_instructions(role, data).rstrip("\n")]
    if cfg["rules"]:
        parts.append("## Project rules\n" + "\n".join(f"- {rule}" for rule in cfg["rules"]))
    if cand.get("instructions"):
        parts.append("## Candidate notes\n" + cand["instructions"].strip())
    fields["developer_instructions"] = "\n\n".join(parts) + "\n"
    return render_agent_toml(fields), resolved


def find_agent_file(root: Path | None, agent: str) -> Path | None:
    places = []
    if root is not None:
        places.append(project_agents_dir(root) / f"{agent}.toml")
    places.append(codex_home() / "agents" / f"{agent}.toml")
    for place in places:
        if place.is_file():
            return place
    return None


# ---------------------------------------------------------------- 岗位选择


def select_dispatch(
    root: Path, cfg: dict[str, Any], role: str, loaded: list[str], requested_agent: str | None = None,
) -> dict[str, Any]:
    """为一个任务选择岗位。无可用岗位时抛出 OrchError（报告缺口，不降级）。"""
    if role not in ROLES:
        raise OrchError(f"未知岗位：{role}")
    templates = load_role_templates()
    loaded_set = set(loaded)
    base = {
        "role": role,
        "config_sha256": cfg.get("sha256"),
        "roles_template_sha256": roles_file_sha256(),
        "degraded_reason": None,
    }
    if cfg["roles"] is not None:
        cards = cfg["roles"].get(role)
        if not cards:
            raise OrchError(f"项目配置未给岗位 {role} 配置候选，缺口需由主控补齐配置；不回退到全局岗位")
        enabled = sorted((c for c in cards if c["priority"] > 0), key=lambda c: (c["priority"], c["order"]))
        if not enabled:
            raise OrchError(f"岗位 {role} 的候选全部停用（priority 0），无可用岗位")
        if requested_agent is not None:
            match = [c for c in cards if c["agent"] == requested_agent]
            if not match:
                raise OrchError(f"requested_agent {requested_agent} 不是岗位 {role} 的候选")
            if match[0]["priority"] == 0:
                raise OrchError(f"requested_agent {requested_agent} 已停用（priority 0），不能绕过")
            enabled = match
        chosen = next((c for c in enabled if c["agent"] in loaded_set), None)
        if chosen is None:
            names = ", ".join(c["agent"] for c in enabled)
            raise OrchError(f"岗位 {role} 的启用候选（{names}）均未在当前会话加载；报告缺口，不降级")
        catalog_info = {"model": None, "reasoning_effort": None, "catalog_verified": None, "catalog_entry_sha256": None}
        if chosen["managed"]:
            catalog = load_catalog(root)
            expected, catalog_info = render_project_agent(role, chosen, cfg, catalog, templates)
            agent_file = project_agents_dir(root) / f"{chosen['agent']}.toml"
            status = agent_file_status(agent_file, expected)
            if status != "current":
                raise OrchError(f"托管岗位文件 {agent_file} 状态为 {status}；请运行 roles.py generate 后新开会话")
        else:
            agent_file = find_agent_file(root, chosen["agent"])
        return dict(
            base,
            mode="named",
            agent_type=chosen["agent"],
            source="project-managed" if chosen["managed"] else "project-reference",
            priority=chosen["priority"],
            declared_model=catalog_info["model"],
            declared_effort=catalog_info["reasoning_effort"],
            catalog_verified=catalog_info["catalog_verified"],
            catalog_entry_sha256=catalog_info["catalog_entry_sha256"],
            agent_file=str(agent_file) if agent_file else None,
            agent_file_sha256=sha256_file(agent_file) if agent_file and agent_file.is_file() else None,
        )

    name = global_agent_name(role)
    if requested_agent is not None and requested_agent != name:
        raise OrchError(f"未配置项目岗位时 requested_agent 只能是 {name}")
    agent_file = codex_home() / "agents" / f"{name}.toml"
    common = dict(
        base,
        source="global",
        priority=None,
        declared_model=None,
        declared_effort=None,
        catalog_verified=None,
        catalog_entry_sha256=None,
        agent_file=str(agent_file),
        agent_file_sha256=sha256_file(agent_file) if agent_file.is_file() else None,
    )
    if name in loaded_set:
        return dict(common, mode="named", agent_type=name)
    if cfg["dispatch"] == "named":
        raise OrchError(f"当前会话未加载 {name}，且 dispatch=named；停止派发")
    return dict(
        common,
        mode="inline",
        agent_type=None,
        source="inline",
        degraded_reason=f"当前会话未加载全局岗位 {name}，改用默认子代理并在合同中携带完整岗位职责",
    )


def dispatch_fingerprint(dispatch: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mode", "role", "agent_type", "source", "priority", "declared_model", "declared_effort",
        "config_sha256", "roles_template_sha256", "agent_file_sha256", "catalog_entry_sha256",
    )
    return {key: dispatch.get(key) for key in keys}


def emit(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()

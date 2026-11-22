"""岗位工具：安装全局 orch_* 岗位、按预设初始化项目配置、生成项目托管岗位、只读诊断。

用法：
  python roles.py install-global [--replace]
  python roles.py init --root <项目> [--preset <名称>]
  python roles.py generate --root <项目>
  python roles.py diff-preset --root <项目> [--preset <名称>]
  python roles.py doctor [--root <项目>]
  python roles.py show --role <岗位>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orchlib as ol  # noqa: E402


def _backup(files: list[Path]) -> Path:
    base = ol.codex_home() / "backups" / ol.SKILL_NAME
    stamp = ol.timestamp_slug()
    target = base / stamp
    suffix = 1
    while target.exists():
        suffix += 1
        target = base / f"{stamp}-{suffix}"
    target.mkdir(parents=True)
    for path in files:
        shutil.copy2(path, target / path.name)
    return target


def install_global(replace: bool) -> dict[str, Any]:
    templates = ol.load_role_templates()
    agents_dir = ol.codex_home() / "agents"
    plan = []
    for role in ol.ROLES:
        path = agents_dir / f"{ol.global_agent_name(role)}.toml"
        text = ol.render_global_agent(role, templates)
        plan.append((role, path, text, ol.agent_file_status(path, text)))
    blocked = [item for item in plan if item[3] in ("modified", "foreign")]
    if blocked and not replace:
        names = ", ".join(f"{item[1].name}（{item[3]}）" for item in blocked)
        raise ol.OrchError(f"以下岗位文件不是本技能生成或已被手改，拒绝覆盖：{names}；确认后加 --replace（会先备份）")
    backup = _backup([item[1] for item in blocked]) if blocked else None
    results = []
    for role, path, text, status in plan:
        if status == "current":
            action = "skipped"
        else:
            ol.write_text_lf(path, text)
            action = {"missing": "created", "outdated": "updated"}.get(status, "replaced")
        results.append({"agent": path.stem, "path": str(path), "previous": status, "action": action, "sha256": ol.sha256_file(path)})
    return {
        "ok": True,
        "agents": results,
        "backup": str(backup) if backup else None,
        "note": "岗位文件需新开 Codex 会话才会加载",
    }


def _project_expected(root: Path, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    expected = []
    errors = []
    if cfg["roles"] is None:
        return expected, errors
    catalog = None
    try:
        catalog = ol.load_catalog(root)
    except ol.OrchError as exc:
        errors.append(str(exc))
    templates = ol.load_role_templates()
    for role, cards in cfg["roles"].items():
        for cand in cards:
            if not cand["managed"]:
                continue
            path = ol.project_agents_dir(root) / f"{cand['agent']}.toml"
            try:
                text, resolved = ol.render_project_agent(role, cand, cfg, catalog, templates)
            except ol.OrchError as exc:
                errors.append(f"{cand['agent']}：{exc}")
                continue
            expected.append({"role": role, "agent": cand["agent"], "path": path, "text": text, "resolved": resolved})
    return expected, errors


def _orphans(root: Path, keep: set[str]) -> list[str]:
    agents_dir = ol.project_agents_dir(root)
    if not agents_dir.is_dir():
        return []
    found = []
    for path in sorted(agents_dir.glob("*.toml")):
        if path.stem in keep:
            continue
        if ol.agent_file_status(path, None) in ("outdated", "modified"):
            found.append(str(path))
    return found


def generate(root: Path) -> dict[str, Any]:
    cfg = ol.load_project_config(root)
    if not cfg["present"]:
        raise ol.OrchError(f"未找到项目编排配置 {ol.project_config_path(root)}")
    if cfg["roles"] is None:
        raise ol.OrchError("项目编排配置没有 roles，无需生成岗位")
    expected, errors = _project_expected(root, cfg)
    if errors:
        raise ol.OrchError("；".join(errors))
    results = []
    refused = []
    for item in expected:
        status = ol.agent_file_status(item["path"], item["text"])
        if status in ("modified", "foreign"):
            refused.append(f"{item['path'].name}（{status}）")
            action = "refused"
        elif status == "current":
            action = "skipped"
        else:
            ol.write_text_lf(item["path"], item["text"])
            action = "created" if status == "missing" else "updated"
        results.append({
            "agent": item["agent"],
            "role": item["role"],
            "path": str(item["path"]),
            "previous": status,
            "action": action,
            "model": item["resolved"]["model"],
            "reasoning_effort": item["resolved"]["reasoning_effort"],
            "catalog_verified": item["resolved"]["catalog_verified"],
        })
    orphans = _orphans(root, {item["agent"] for item in expected})
    return {
        "ok": not refused,
        "agents": results,
        "refused": refused,
        "removed_from_config": orphans,
        "note": "生成的岗位需新开 Codex 会话才会加载；已从配置移除的托管文件只报告不删除",
    }


def doctor(root: Path | None) -> dict[str, Any]:
    templates = ol.load_role_templates()
    report: dict[str, Any] = {"ok": True, "codex_home": str(ol.codex_home()), "global_agents": {}}
    for role in ol.ROLES:
        name = ol.global_agent_name(role)
        path = ol.codex_home() / "agents" / f"{name}.toml"
        report["global_agents"][name] = ol.agent_file_status(path, ol.render_global_agent(role, templates))
    try:
        location = ol.catalog_location(root)
        catalog = ol.load_catalog(root) if location else None
        report["catalog"] = {
            "configured": location is not None,
            "path": str(location["path"]) if location else None,
            "from_config": str(location["config"]) if location else None,
            "models": len(catalog["models"]) if catalog else 0,
        }
    except ol.OrchError as exc:
        report["ok"] = False
        report["catalog"] = {"error": str(exc)}
    try:
        report["native_thread_limit"] = ol.native_thread_limit(root)
    except ol.OrchError as exc:
        report["ok"] = False
        report["native_thread_limit"] = {"error": str(exc)}
    profile = None
    try:
        profile = ol.load_model_profile()
        report["model_profile"] = _profile_summary(profile, root)
    except ol.OrchError as exc:
        report["ok"] = False
        report["model_profile"] = {"path": str(ol.profile_path()), "error": str(exc)}
    report["presets"] = {}
    for name in ol.list_presets():
        try:
            ol.load_preset(root or ol.codex_home(), name)
            report["presets"][name] = "valid"
        except ol.OrchError as exc:
            report["ok"] = False
            report["presets"][name] = str(exc)
    if root is None:
        return report
    project: dict[str, Any] = {"root": str(root)}
    try:
        cfg = ol.load_project_config(root)
        project["config"] = {
            "present": cfg["present"],
            "valid": True,
            "dispatch": cfg["dispatch"],
            "has_roles": cfg["roles"] is not None,
            "sha256": cfg["sha256"],
        }
        expected, errors = _project_expected(root, cfg)
        project["managed_agents"] = {
            item["agent"]: {
                "status": ol.agent_file_status(item["path"], item["text"]),
                "model": item["resolved"]["model"],
                "reasoning_effort": item["resolved"]["reasoning_effort"],
                "catalog_verified": item["resolved"]["catalog_verified"],
            }
            for item in expected
        }
        project["render_errors"] = errors
        project["removed_from_config"] = _orphans(root, {item["agent"] for item in expected})
        project["advisories"] = _advisories(cfg, profile)
        drift = [name for name, info in project["managed_agents"].items() if info["status"] != "current"]
        if errors or drift:
            report["ok"] = False
    except ol.OrchError as exc:
        report["ok"] = False
        project["config"] = {"valid": False, "error": str(exc)}
    report["project"] = project
    return report


def _profile_summary(profile: dict[str, Any] | None, root: Path | None) -> dict[str, Any]:
    if profile is None:
        return {"path": str(ol.profile_path()), "present": False}
    summary: dict[str, Any] = {"path": profile["path"], "present": True, "models": len(profile["models"])}
    try:
        catalog = ol.load_catalog(root) if ol.catalog_location(root) else None
    except ol.OrchError:
        catalog = None
    if catalog is not None:
        summary["not_in_catalog"] = sorted(slug for slug in profile["models"] if slug not in catalog["models"])
    return summary


def _top_enabled(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    enabled = sorted((c for c in cards if c["priority"] > 0), key=lambda c: (c["priority"], c["order"]))
    return enabled[0] if enabled else None


def _family(cand: dict[str, Any], profile: dict[str, Any] | None) -> str | None:
    """模型家族：未指定模型的托管候选和全局 orch_* 继承会话模型，记为 inherit；无法判断时返回 None。"""
    if cand.get("model"):
        if profile and cand["model"] in profile["models"]:
            return profile["models"][cand["model"]]["family"]
        return None
    if cand["managed"] or cand["agent"].startswith(ol.GLOBAL_PREFIX + "_"):
        return "inherit"
    return None


def _advisories(cfg: dict[str, Any], profile: dict[str, Any] | None) -> list[str]:
    """只读的选型建议：不影响 ok，也不改变选择结果。"""
    if cfg["roles"] is None:
        return []
    templates = ol.load_role_templates()
    notes: list[str] = []
    if profile is None:
        notes.append(f"未找到模型档案 {ol.profile_path()}，无法检查候选模型与岗位是否匹配")
    for role, cards in cfg["roles"].items():
        needs = set(templates["roles"][role].get("needs", []))
        for cand in cards:
            model = cand.get("model")
            if cand["priority"] == 0 or not model or profile is None:
                continue
            entry = profile["models"].get(model)
            if entry is None:
                notes.append(f"{role}/{cand['agent']}：模型 {model} 不在模型档案中")
                continue
            if entry.get("retired"):
                notes.append(f"{role}/{cand['agent']}：模型 {model} 在档案中标为 retired，建议把该候选设为 priority 0")
            suited = entry.get("suited_roles", [])
            if suited and role not in suited:
                notes.append(f"{role}/{cand['agent']}：档案中 {model} 的 suited_roles 不含 {role}")
            caps = set(entry.get("capabilities", []))
            if caps and needs - caps:
                notes.append(f"{role}/{cand['agent']}：{model} 缺少岗位所需能力 {', '.join(sorted(needs - caps))}")
    for role in ol.ROLES:
        mine = _top_enabled(cfg["roles"].get(role, []))
        if mine is None:
            continue
        for other in templates["roles"][role].get("independent_from", []):
            theirs = _top_enabled(cfg["roles"].get(other, []))
            if theirs is None:
                continue
            fam_a, fam_b = _family(mine, profile), _family(theirs, profile)
            if fam_a is not None and fam_a == fam_b:
                shown = "都继承会话模型" if fam_a == "inherit" else f"同属模型家族 {fam_a}"
                notes.append(f"{role} 首选 {mine['agent']} 与 {other} 首选 {theirs['agent']} {shown}，独立性不足")
    return notes


def init(root: Path, name: str) -> dict[str, Any]:
    target = ol.project_config_path(root)
    if target.exists():
        raise ol.OrchError(f"{target} 已存在；init 不覆盖现有配置，请直接编辑，或用 diff-preset 对比预设")
    data, digest, path = ol.load_preset(root, name)
    out: dict[str, Any] = {"schema": data["schema"], "preset": {"name": name, "sha256": digest}}
    out.update((key, value) for key, value in data.items() if key != "schema")
    cfg = ol.validate_project_config(root, out)
    ol.write_text_lf(target, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    roles_summary = {
        role: [c["agent"] for c in sorted(cards, key=lambda c: (c["priority"], c["order"])) if c["priority"] > 0]
        for role, cards in (cfg["roles"] or {}).items()
    }
    return {
        "ok": True,
        "path": str(target),
        "preset": {"name": name, "path": str(path), "sha256": digest},
        "enabled_candidates": roles_summary,
        "next": "按项目需要修改 rules 与 role_focus，再运行 roles.py generate --root <项目>，然后新开 Codex 会话",
    }


CANDIDATE_FIELDS = ("priority", "model", "reasoning_effort", "instructions")


def diff_preset(root: Path, name: str | None) -> dict[str, Any]:
    path = ol.project_config_path(root)
    if not path.exists():
        raise ol.OrchError(f"未找到项目编排配置 {path}")
    raw = ol.read_json_file(path, ".codex/orchestration.json")
    cfg = ol.validate_project_config(root, raw)
    origin = cfg["preset"]
    name = name or (origin["name"] if origin else None)
    if not name:
        raise ol.OrchError("项目配置没有记录来源预设，请用 --preset 指定要对比的预设")
    data, digest, preset_path = ol.load_preset(root, name)
    roles_diff: dict[str, Any] = {}
    project_roles = raw.get("roles", {})
    preset_roles = data.get("roles", {})
    for role in ol.ROLES:
        mine = {c["agent"]: c for c in project_roles.get(role, {}).get("candidates", [])}
        theirs = {c["agent"]: c for c in preset_roles.get(role, {}).get("candidates", [])}
        changed = {}
        for agent in sorted(set(mine) & set(theirs)):
            fields = {f: [mine[agent].get(f), theirs[agent].get(f)] for f in CANDIDATE_FIELDS if mine[agent].get(f) != theirs[agent].get(f)}
            if fields:
                changed[agent] = fields
        entry = {
            "only_in_preset": sorted(set(theirs) - set(mine)),
            "only_in_project": sorted(set(mine) - set(theirs)),
            "changed": changed,
        }
        if any(entry.values()):
            roles_diff[role] = entry
    skip = {"schema", "preset", "roles"}
    other = sorted(k for k in (set(raw) | set(data)) - skip if raw.get(k) != data.get(k))
    return {
        "ok": True,
        "preset": {"name": name, "path": str(preset_path), "sha256": digest},
        "recorded_sha256": origin["sha256"] if origin and origin["name"] == name else None,
        "preset_changed_since_init": bool(origin and origin["name"] == name and origin["sha256"] != digest),
        "roles": roles_diff,
        "other_keys_differ": other,
        "note": "只读对比，字段值为 [项目, 预设]；需要同步时手动修改项目配置，再运行 generate",
    }


def main(argv: list[str] | None = None) -> int:
    ol.force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="roles.py", description="多代理编排岗位工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install-global", help="安装或更新全局 orch_* 岗位")
    p_install.add_argument("--replace", action="store_true", help="备份后替换手改或外来文件")
    p_init = sub.add_parser("init", help="用用户级预设创建项目配置 .codex/orchestration.json")
    p_init.add_argument("--root", required=True)
    p_init.add_argument("--preset", default="default")
    p_gen = sub.add_parser("generate", help="按项目配置生成托管岗位")
    p_gen.add_argument("--root", required=True)
    p_diff = sub.add_parser("diff-preset", help="只读对比项目配置与预设")
    p_diff.add_argument("--root", required=True)
    p_diff.add_argument("--preset")
    p_doc = sub.add_parser("doctor", help="只读诊断")
    p_doc.add_argument("--root")
    p_show = sub.add_parser("show", help="显示岗位的完整职责")
    p_show.add_argument("--role", required=True, choices=ol.ROLES)
    args = parser.parse_args(argv)
    try:
        if args.command == "install-global":
            result = install_global(args.replace)
        elif args.command == "init":
            result = init(Path(args.root).resolve(), args.preset)
        elif args.command == "generate":
            result = generate(Path(args.root).resolve())
        elif args.command == "diff-preset":
            result = diff_preset(Path(args.root).resolve(), args.preset)
        elif args.command == "doctor":
            result = doctor(Path(args.root).resolve() if args.root else None)
        else:
            result = {"ok": True, "role": args.role, "instructions": ol.role_instructions(args.role)}
    except ol.OrchError as exc:
        ol.emit({"ok": False, "error": str(exc)})
        return 1
    ol.emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

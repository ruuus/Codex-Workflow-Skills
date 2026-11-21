"""岗位工具：安装全局 orch_* 岗位、生成项目托管岗位、只读诊断。

用法：
  python roles.py install-global [--replace]
  python roles.py generate --root <项目>
  python roles.py doctor [--root <项目>]
  python roles.py show --role <岗位>
"""

from __future__ import annotations

import argparse
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
        drift = [name for name, info in project["managed_agents"].items() if info["status"] != "current"]
        if errors or drift:
            report["ok"] = False
    except ol.OrchError as exc:
        report["ok"] = False
        project["config"] = {"valid": False, "error": str(exc)}
    report["project"] = project
    return report


def main(argv: list[str] | None = None) -> int:
    ol.force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="roles.py", description="多代理编排岗位工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install-global", help="安装或更新全局 orch_* 岗位")
    p_install.add_argument("--replace", action="store_true", help="备份后替换手改或外来文件")
    p_gen = sub.add_parser("generate", help="按项目配置生成托管岗位")
    p_gen.add_argument("--root", required=True)
    p_doc = sub.add_parser("doctor", help="只读诊断")
    p_doc.add_argument("--root")
    p_show = sub.add_parser("show", help="显示岗位的完整职责")
    p_show.add_argument("--role", required=True, choices=ol.ROLES)
    args = parser.parse_args(argv)
    try:
        if args.command == "install-global":
            result = install_global(args.replace)
        elif args.command == "generate":
            result = generate(Path(args.root).resolve())
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

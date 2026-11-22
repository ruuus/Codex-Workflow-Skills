"""把本仓库 skills/<skill> 安装到 $CODEX_HOME/skills/<skill>（默认 ~/.codex/skills），并用清单追踪已安装文件。

用法：python tools/install_skill.py <skill> [--dest 父目录] [--replace]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parent.parent
MANIFEST = ".skill-install.json"


class InstallError(Exception):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ignored(rel: Path) -> bool:
    return "__pycache__" in rel.parts or rel.suffix in (".pyc", ".pyo", ".pyd")


def scan(folder: Path) -> dict[str, str]:
    files = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(folder)
        if _ignored(rel) or rel.as_posix() == MANIFEST:
            continue
        files[rel.as_posix()] = _sha256(path)
    return files


def source_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-c", f"safe.directory={REPO.as_posix()}", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def backup(target: Path, skill: str) -> Path:
    base = codex_home() / "backups" / skill
    stamp = _dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    dest = base / stamp
    index = 1
    while dest.exists():
        index += 1
        dest = base / f"{stamp}-{index}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(dest))
    return dest


def copy_files(source: Path, target: Path, files: dict[str, str]) -> None:
    for rel in files:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, dst)


def write_manifest(target: Path, skill: str, files: dict[str, str]) -> None:
    data = {"skill": skill, "source_commit": source_commit(), "files": files}
    with open(target / MANIFEST, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def install(skill: str, dest_parent: Path, replace: bool) -> dict:
    source = REPO / "skills" / skill
    if not (source / "SKILL.md").is_file():
        raise InstallError(f"仓库中没有技能 {skill}")
    target = dest_parent / skill
    wanted = scan(source)
    if not target.exists():
        copy_files(source, target, wanted)
        write_manifest(target, skill, wanted)
        return {"ok": True, "action": "installed", "target": str(target), "files": len(wanted)}

    present = scan(target)
    recorded = None
    recorded_commit = None
    manifest_path = target / MANIFEST
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            recorded = manifest.get("files")
            recorded_commit = manifest.get("source_commit")
        except (OSError, ValueError, AttributeError):
            recorded = None
    if recorded is None or recorded != present:
        reason = "目标目录没有安装清单" if recorded is None else "目标目录含清单外文件或文件已被改动"
        if not replace:
            raise InstallError(f"{reason}，拒绝覆盖 {target}；确认后加 --replace（会先备份）")
        saved = backup(target, skill)
        copy_files(source, target, wanted)
        write_manifest(target, skill, wanted)
        return {"ok": True, "action": "replaced", "target": str(target), "backup": str(saved), "reason": reason}

    if present == wanted:
        if recorded_commit != source_commit():
            write_manifest(target, skill, wanted)
        return {"ok": True, "action": "unchanged", "target": str(target)}
    for rel in set(present) - set(wanted):
        (target / rel).unlink()
    copy_files(source, target, wanted)
    for folder in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    write_manifest(target, skill, wanted)
    return {"ok": True, "action": "updated", "target": str(target), "files": len(wanted)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="安装本仓库中的 Codex 技能")
    parser.add_argument("skill")
    parser.add_argument("--dest", help="安装到的父目录，默认 $CODEX_HOME/skills")
    parser.add_argument("--replace", action="store_true", help="备份后替换不受清单管理或被改动的目录")
    args = parser.parse_args(argv)
    dest = Path(args.dest).expanduser() if args.dest else codex_home() / "skills"
    try:
        result = install(args.skill, dest, args.replace)
    except InstallError as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

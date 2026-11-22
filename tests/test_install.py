import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.dont_write_bytecode = True

import install_skill  # noqa: E402


class InstallSkill(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / "repo"
        self.skill = self.repo / "skills" / "demo"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        (self.skill / "scripts" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (self.skill / "scripts" / "__pycache__").mkdir()
        (self.skill / "scripts" / "__pycache__" / "a.cpython.pyc").write_bytes(b"x")
        self.dest = self.base / "skills"
        self.home = self.base / "codex-home"
        for patcher in (mock.patch.object(install_skill, "REPO", self.repo),
                        mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, *extra, ok=True):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = install_skill.main(["demo", "--dest", str(self.dest), *extra])
        result = json.loads(buf.getvalue())
        self.assertEqual(code == 0, ok, result)
        return result

    @property
    def target(self):
        return self.dest / "demo"

    def test_first_install_and_unchanged(self):
        self.assertEqual(self.install()["action"], "installed")
        manifest = json.loads((self.target / ".skill-install.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["files"]), {"SKILL.md", "scripts/a.py"})
        self.assertIn("source_commit", manifest)
        self.assertFalse((self.target / "scripts" / "__pycache__").exists())
        self.assertEqual(self.install()["action"], "unchanged")

    def test_default_dest_is_codex_home_skills(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = install_skill.main(["demo"])
        result = json.loads(buf.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(Path(result["target"]), self.home / "skills" / "demo")
        self.assertTrue((self.home / "skills" / "demo" / "SKILL.md").exists())

    def test_update_follows_manifest(self):
        self.install()
        (self.skill / "scripts" / "a.py").unlink()
        (self.skill / "scripts" / "b.py").write_text("print(2)\n", encoding="utf-8")
        (self.target / "scripts" / "__pycache__").mkdir()
        (self.target / "scripts" / "__pycache__" / "a.pyc").write_bytes(b"y")
        self.assertEqual(self.install()["action"], "updated")
        self.assertFalse((self.target / "scripts" / "a.py").exists())
        self.assertTrue((self.target / "scripts" / "b.py").exists())

    def test_refuses_modified_or_unknown_and_replace_backs_up(self):
        self.install()
        (self.target / "scripts" / "a.py").write_text("tampered\n", encoding="utf-8")
        self.assertIn("--replace", self.install(ok=False)["error"])
        (self.target / "scripts" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (self.target / "extra.txt").write_text("x", encoding="utf-8")
        self.install(ok=False)
        result = self.install("--replace")
        self.assertEqual(result["action"], "replaced")
        backup = Path(result["backup"])
        self.assertTrue(backup.is_relative_to(self.home / "backups" / "demo"))
        self.assertFalse(backup.is_relative_to(self.dest))
        self.assertTrue((backup / "extra.txt").is_file())
        self.assertFalse((self.target / "extra.txt").exists())

    def test_refuses_unmanaged_directory(self):
        self.target.mkdir(parents=True)
        (self.target / "SKILL.md").write_text("other", encoding="utf-8")
        self.assertIn("清单", self.install(ok=False)["error"])

    def test_unknown_skill(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = install_skill.main(["missing", "--dest", str(self.dest)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

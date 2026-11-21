import json
import re
import sys
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "multi-agent-orchestration"
sys.path.insert(0, str(SKILL / "scripts"))
sys.dont_write_bytecode = True

import orchlib as ol  # noqa: E402


def frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "缺少 frontmatter"
    result = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        result[key.strip()] = value.strip()
    return result


def yaml_value(text: str, key: str) -> str:
    match = re.search(rf'^\s*{key}:\s*"(.*)"\s*$', text, re.M)
    assert match, f"openai.yaml 缺少 {key}"
    return match.group(1)


class SkillPackage(unittest.TestCase):
    def test_frontmatter(self):
        meta = frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(set(meta), {"name", "description", "license"})
        self.assertEqual(meta["name"], SKILL.name)
        self.assertEqual(meta["license"], "MIT")
        self.assertLessEqual(len(meta["description"]), 1024)
        self.assertNotRegex(meta["description"], "[<>]")

    def test_licenses_match(self):
        root = (REPO / "LICENSE").read_bytes()
        self.assertEqual(root, (SKILL / "LICENSE.txt").read_bytes())
        self.assertIn(b"Copyright (c) 2026 ruuus", root)

    def test_roles_toml(self):
        data = tomllib.loads((SKILL / "assets" / "roles.toml").read_text(encoding="utf-8"))
        self.assertEqual(tuple(data["roles"]), ol.ROLES)
        self.assertEqual(ol.load_role_templates(), data)

    def test_example_config_validates(self):
        data = json.loads((SKILL / "assets" / "orchestration.example.json").read_text(encoding="utf-8"))
        cfg = ol.validate_project_config(REPO, data)
        self.assertEqual(set(cfg["roles"]), set(ol.ROLES))

    def test_openai_yaml(self):
        text = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$multi-agent-orchestration", yaml_value(text, "default_prompt"))
        self.assertTrue(25 <= len(yaml_value(text, "short_description")) <= 64)
        self.assertTrue(yaml_value(text, "display_name"))

    def test_reference_links_exist(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for link in re.findall(r"\]\((references/[^)]+)\)", text):
            self.assertTrue((SKILL / link).is_file(), link)


if __name__ == "__main__":
    unittest.main()

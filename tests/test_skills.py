"""技能发现测试：扫描 .fool/skills/，把索引拼进 system prompt。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402
import skills  # noqa: E402


SKILL_MD = """---
name: demo-skill
description: 一个演示技能，用来算两数之和。
---

# 演示技能

用法：`python scripts/demo.py 1 2`
"""

SKILL_MD_MULTILINE = """---
name: long-one
description: 第一行说明
  第二行接上
---

正文
"""

SKILL_MD_NO_FRONTMATTER = """# 没有 frontmatter 的技能

正文
"""


class FrontmatterTest(unittest.TestCase):
    def test_parses_name_and_description(self) -> None:
        values = skills._parse_frontmatter(SKILL_MD)
        self.assertEqual(values["name"], "demo-skill")
        self.assertEqual(values["description"], "一个演示技能，用来算两数之和。")

    def test_joins_multiline_description(self) -> None:
        values = skills._parse_frontmatter(SKILL_MD_MULTILINE)
        self.assertEqual(values["name"], "long-one")
        self.assertIn("第一行说明", values["description"])
        self.assertIn("第二行接上", values["description"])

    def test_missing_frontmatter_returns_empty(self) -> None:
        self.assertEqual(skills._parse_frontmatter(SKILL_MD_NO_FRONTMATTER), {})

    def test_strips_quotes(self) -> None:
        text = '---\nname: "quoted"\ndescription: \'single\'\n---\n'
        values = skills._parse_frontmatter(text)
        self.assertEqual(values["name"], "quoted")
        self.assertEqual(values["description"], "single")


class FindSkillsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def _make_skill(self, folder: str, manifest: str, scripts=("demo.py",)):
        skill_dir = self.root / ".fool" / "skills" / folder
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")
        if scripts:
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir()
            for script in scripts:
                (scripts_dir / script).write_text("print('x')\n", encoding="utf-8")
        return skill_dir

    def test_no_skills_directory_returns_empty(self) -> None:
        self.assertEqual(skills.find_skills(self.root), [])
        self.assertEqual(skills.index_for(self.root), "")

    def test_finds_skill_with_scripts(self) -> None:
        self._make_skill("demo-skill", SKILL_MD)
        found = skills.find_skills(self.root)
        self.assertEqual(len(found), 1)
        skill = found[0]
        self.assertEqual(skill.name, "demo-skill")
        self.assertEqual(skill.description, "一个演示技能，用来算两数之和。")
        self.assertEqual(skill.manifest, ".fool/skills/demo-skill/SKILL.md")
        self.assertEqual(skill.scripts, [".fool/skills/demo-skill/scripts/demo.py"])

    def test_skill_without_frontmatter_falls_back_to_folder_name(self) -> None:
        self._make_skill("plain", SKILL_MD_NO_FRONTMATTER, scripts=())
        found = skills.find_skills(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "plain")
        self.assertEqual(found[0].description, "")

    def test_directory_without_manifest_is_ignored(self) -> None:
        (self.root / ".fool" / "skills" / "broken").mkdir(parents=True)
        found = skills.find_skills(self.root)
        self.assertEqual(found, [])

    def test_index_text_lists_name_description_and_script(self) -> None:
        self._make_skill("demo-skill", SKILL_MD)
        index = skills.index_for(self.root)
        self.assertIn("demo-skill", index)
        self.assertIn("一个演示技能", index)
        self.assertIn(".fool/skills/demo-skill/SKILL.md", index)
        self.assertIn(".fool/skills/demo-skill/scripts/demo.py", index)

    def test_index_of_real_skill_package(self) -> None:
        """仓库自带的技能包应该能被扫到。"""
        root = Path(__file__).resolve().parents[1]
        found = skills.find_skills(root)
        names = [s.name for s in found]
        self.assertIn("fibonacci-bignum", names)
        index = skills.index_for(root)
        self.assertIn("fibonacci-bignum", index)
        self.assertIn("fibonacci.py", index)


class SystemPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def test_prompt_without_skills_is_unchanged(self) -> None:
        self.assertEqual(
            mini_agent.build_system_prompt(self.root), mini_agent.SYSTEM_PROMPT
        )

    def test_prompt_with_skills_contains_index(self) -> None:
        skill_dir = self.root / ".fool" / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        prompt = mini_agent.build_system_prompt(self.root)
        self.assertIn("demo-skill", prompt)
        self.assertIn("一个演示技能", prompt)
        # 原来的工作原则不能丢
        self.assertIn("先读后改", prompt)
        self.assertIn("工作原则", prompt)
        # 索引要排在工作原则前面，模型先看到有什么能力
        self.assertLess(prompt.index("demo-skill"), prompt.index("工作原则"))

    def test_agent_sends_prompt_with_skills(self) -> None:
        """真的调模型时，system 里要带上技能索引。"""
        skill_dir = self.root / ".fool" / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        original = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        try:
            client = _FakeClient()
            mini_agent.run_agent("hi", client=client, verbose=False)
        finally:
            mini_agent.WORKSPACE = original

        self.assertIn("demo-skill", client.messages.calls[0]["system"])


class _FakeResponse:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse([{"type": "text", "text": "ok"}], "end_turn")


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


if __name__ == "__main__":
    unittest.main(verbosity=2)

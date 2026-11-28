"""前端渲染测试：流式增量怎么画到界面上（需要 node，没有则跳过）。

用 tests/webapp_harness.js 给 web/app.js 套一层最小 DOM，
按事件序列跑一遍，检查最后界面长什么样。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "app.js"
HARNESS = ROOT / "tests" / "webapp_harness.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE and APP_JS.is_file() and HARNESS.is_file(),
                     "需要 node、web/app.js 和 tests/webapp_harness.js")
class WebAppRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe = subprocess.run(
            [NODE, str(HARNESS), str(APP_JS), "[]"],
            capture_output=True,
            timeout=20,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest(probe.stderr.decode("utf-8", "replace"))

    def render(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        result = subprocess.run(
            [NODE, str(HARNESS), str(APP_JS), json.dumps(events, ensure_ascii=False)],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0, result.stderr.decode("utf-8", "replace")
        )
        return json.loads(result.stdout.decode("utf-8"))

    # ── 流式文本 ─────────────────────────────────────────────

    def test_text_deltas_render_into_answer(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "text_delta", "text": "你好"},
            {"type": "text_delta", "text": "世界"},
            {"type": "final", "text": "你好世界"},
        ])
        self.assertIn("你好世界", state["answerHtml"])
        self.assertEqual(state["notes"], [])
        self.assertEqual(state["steps"], 1)

    def test_final_answer_is_rendered_as_markdown(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "text_delta", "text": "# 标题"},
            {"type": "final", "text": "# 标题"},
        ])
        self.assertIn("<h1>", state["answerHtml"])
        self.assertIn("标题", state["answerHtml"])

    def test_intermediate_text_moves_into_step_notes(self) -> None:
        """工具调用前的过渡性文字不能留在回答区，要挪进步骤明细。"""
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "text_delta", "text": "我先看看文件。"},
            {"type": "tool_call", "id": "call_1", "name": "read",
             "input": {"path": "a.txt"}},
            {"type": "tool_result", "id": "call_1", "name": "read",
             "output": "hello"},
            {"type": "step", "step": 2},
            {"type": "text_delta", "text": "读完了"},
            {"type": "final", "text": "读完了"},
        ])
        # 回答区只剩最终回答
        self.assertIn("读完了", state["answerHtml"])
        self.assertNotIn("我先看看文件", state["answerHtml"])
        # 过渡性文字进了步骤明细
        self.assertIn("我先看看文件。", state["notes"])
        self.assertEqual(state["steps"], 2)

    def test_leftover_text_is_flushed_on_next_step(self) -> None:
        """没有 tool_call 就直接进下一步时，上一步的文字也要挪走。"""
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "text_delta", "text": "中间的话"},
            {"type": "step", "step": 2},
            {"type": "text_delta", "text": "最终答案"},
            {"type": "final", "text": "最终答案"},
        ])
        self.assertNotIn("中间的话", state["answerHtml"])
        self.assertIn("中间的话", state["notes"])
        self.assertIn("最终答案", state["answerHtml"])

    # ── 思考 ─────────────────────────────────────────────────

    def test_thinking_deltas_accumulate_into_one_block(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "thinking_delta", "text": "先想"},
            {"type": "thinking_delta", "text": "一下"},
            {"type": "text_delta", "text": "答案"},
            {"type": "final", "text": "答案"},
        ])
        self.assertEqual(len(state["thinking"]), 1)
        self.assertIn("先想一下", state["thinking"][0])

    # ── 非流式（老协议）事件仍然要能画出来 ────────────────────

    def test_legacy_thinking_and_assistant_text_still_render(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "thinking", "text": "整块思考"},
            {"type": "assistant_text", "text": "过渡性文字"},
            {"type": "final", "text": "最终"},
        ])
        self.assertIn("整块思考", state["thinking"][0])
        self.assertIn("过渡性文字", state["notes"])
        self.assertIn("最终", state["answerHtml"])

    def test_tool_cards_show_name_and_result(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "tool_call", "id": "call_1", "name": "bash",
             "input": {"command": "ls"}},
            {"type": "tool_result", "id": "call_1", "name": "bash",
             "output": "file.txt"},
        ])
        self.assertEqual(len(state["tools"]), 1)
        self.assertEqual(state["tools"][0]["name"], "bash")
        self.assertEqual(state["tools"][0]["status"], "完成")

    def test_error_event_is_shown(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "error", "message": "调用模型失败：401"},
        ])
        self.assertTrue(any("401" in text for text in state["errors"]))

    # ── 操作确认卡片 ─────────────────────────────────────────

    def test_confirm_card_shows_allow_and_deny(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "confirm", "id": "c1", "name": "bash",
             "input": {"command": "rm -rf x"}},
        ])
        self.assertEqual(len(state["confirms"]), 1)
        card = state["confirms"][0]
        self.assertEqual(card["buttons"], ["允许", "拒绝"])
        self.assertIn("rm -rf x", card["desc"])

    def test_clicking_allow_updates_the_card(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "confirm", "id": "c1", "name": "write",
             "input": {"path": "a.txt"}},
            {"type": "__click", "id": "c1", "approved": True},
        ])
        self.assertIn("已允许", state["confirms"][0]["status"])

    def test_clicking_deny_updates_the_card(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "confirm", "id": "c1", "name": "write",
             "input": {"path": "a.txt"}},
            {"type": "__click", "id": "c1", "approved": False},
            {"type": "confirm_result", "id": "c1", "approved": False},
        ])
        self.assertIn("已拒绝", state["confirms"][0]["status"])

    def test_denied_tool_is_marked(self) -> None:
        state = self.render([
            {"type": "step", "step": 1},
            {"type": "tool_call", "id": "call_1", "name": "bash",
             "input": {"command": "ls"}},
            {"type": "tool_denied", "id": "call_1", "name": "bash"},
        ])
        self.assertEqual(len(state["tools"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

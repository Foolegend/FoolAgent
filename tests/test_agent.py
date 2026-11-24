"""Agent loop 测试：注入假 client，不需要 API key 也能跑完整流程。"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402


class FakeResponse:
    """模拟 anthropic 的响应对象。"""

    def __init__(self, content: List[Dict[str, Any]], stop_reason: str):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, responses: List[FakeResponse]):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("假 client 的响应已用完，说明 loop 跑超了预期")
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: List[FakeResponse]):
        self.messages = FakeMessages(responses)


def tool_use_block(
    name: str, arguments: Dict[str, Any], block_id: str = "call_1"
) -> Dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def thinking_block(text: str) -> Dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "sig"}


class AgentLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

    def _restore_workspace(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace

    def test_single_turn_returns_text(self) -> None:
        client = FakeClient([FakeResponse([text_block("你好")], "end_turn")])
        result = mini_agent.run_agent("hi", client=client, verbose=False)
        self.assertEqual(result, "你好")

    def test_thinking_block_before_text_is_ignored(self) -> None:
        """MiniMax 等模型会先返回 thinking 块，不能混进最终回复。"""
        client = FakeClient(
            [FakeResponse([thinking_block("想一下"), text_block("答案")], "end_turn")]
        )
        result = mini_agent.run_agent("hi", client=client, verbose=False)
        self.assertEqual(result, "答案")

    def test_tool_call_executes_and_returns_result_to_model(self) -> None:
        (self.root / "a.txt").write_text("content\n", encoding="utf-8")
        client = FakeClient(
            [
                FakeResponse([tool_use_block("read", {"path": "a.txt"})], "tool_use"),
                FakeResponse([text_block("读完了")], "end_turn"),
            ]
        )
        result = mini_agent.run_agent("读文件", client=client, verbose=False)
        self.assertEqual(result, "读完了")

        # 第二次请求必须带上第一次工具调用的结果
        second_call = client.messages.calls[1]
        tool_result_msg = second_call["messages"][-1]
        self.assertEqual(tool_result_msg["role"], "user")
        self.assertEqual(tool_result_msg["content"][0]["type"], "tool_result")
        self.assertEqual(tool_result_msg["content"][0]["tool_use_id"], "call_1")
        self.assertIn("content", tool_result_msg["content"][0]["content"])

    def test_write_then_read_roundtrip(self) -> None:
        client = FakeClient(
            [
                FakeResponse(
                    [tool_use_block("write", {"path": "out.txt", "content": "hi"})],
                    "tool_use",
                ),
                FakeResponse([tool_use_block("read", {"path": "out.txt"})], "tool_use"),
                FakeResponse([text_block("done")], "end_turn"),
            ]
        )
        result = mini_agent.run_agent("写再读", client=client, verbose=False)
        self.assertEqual(result, "done")
        self.assertEqual((self.root / "out.txt").read_text(encoding="utf-8"), "hi")

    def test_unknown_tool_returns_error_to_model(self) -> None:
        client = FakeClient(
            [
                FakeResponse([tool_use_block("nuke", {})], "tool_use"),
                FakeResponse([text_block("好的")], "end_turn"),
            ]
        )
        mini_agent.run_agent("x", client=client, verbose=False)
        tool_result = client.messages.calls[1]["messages"][-1]["content"][0]
        self.assertIn("未知工具", tool_result["content"])

    def test_tool_error_is_reported_to_model_without_crashing(self) -> None:
        client = FakeClient(
            [
                FakeResponse(
                    [tool_use_block("read", {"path": "missing.txt"})], "tool_use"
                ),
                FakeResponse([text_block("ok")], "end_turn"),
            ]
        )
        mini_agent.run_agent("x", client=client, verbose=False)
        tool_result = client.messages.calls[1]["messages"][-1]["content"][0]
        self.assertIn("错误", tool_result["content"])

    def test_max_loop_stops_runaway_agent(self) -> None:
        client = FakeClient(
            [FakeResponse([tool_use_block("read", {"path": "a.txt"})], "tool_use")] * 5
        )
        result = _run_with_max_loop(client, 3)
        self.assertIn("达到最大循环次数", result)
        self.assertEqual(len(client.messages.calls), 3)

    def test_api_error_is_surfaced(self) -> None:
        class Boom:
            class messages:  # noqa: N801 - 模拟 SDK 结构
                @staticmethod
                def create(**_: Any) -> Any:
                    raise RuntimeError("401 Unauthorized")

        result = mini_agent.run_agent("x", client=Boom(), verbose=False)
        self.assertIn("调用模型失败", result)
        self.assertIn("401", result)

    def test_tools_and_system_are_passed_to_api(self) -> None:
        client = FakeClient([FakeResponse([text_block("ok")], "end_turn")])
        mini_agent.run_agent("x", client=client, verbose=False)
        call = client.messages.calls[0]
        self.assertEqual(
            sorted(schema["name"] for schema in call["tools"]),
            ["bash", "edit", "read", "write"],
        )
        self.assertIn("read", call["system"])
        self.assertEqual(call["messages"][0]["role"], "user")

    def test_verbose_output_shows_tool_call(self) -> None:
        client = FakeClient(
            [
                FakeResponse([tool_use_block("read", {"path": "a.txt"})], "tool_use"),
                FakeResponse([text_block("done")], "end_turn"),
            ]
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            mini_agent.run_agent("x", client=client, verbose=True)
        output = buffer.getvalue()
        self.assertIn("[Step 1]", output)
        self.assertIn("read", output)

    # ── 多轮对话 ──────────────────────────────────────────────

    def test_history_is_shared_across_turns(self) -> None:
        """传入同一个 history，第二轮请求必须带上第一轮的问答。"""
        client = FakeClient(
            [
                FakeResponse([text_block("第一轮")], "end_turn"),
                FakeResponse([text_block("第二轮")], "end_turn"),
            ]
        )
        history: List[Dict[str, Any]] = []
        first = mini_agent.run_agent("你好", client=client, verbose=False, history=history)
        second = mini_agent.run_agent("继续", client=client, verbose=False, history=history)

        self.assertEqual(first, "第一轮")
        self.assertEqual(second, "第二轮")
        sent = client.messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])
        self.assertEqual(sent[0]["content"], "你好")
        self.assertEqual(sent[1]["content"][0]["text"], "第一轮")
        self.assertEqual(sent[2]["content"], "继续")

    def test_omitting_history_keeps_turns_isolated(self) -> None:
        client = FakeClient(
            [
                FakeResponse([text_block("a")], "end_turn"),
                FakeResponse([text_block("b")], "end_turn"),
            ]
        )
        mini_agent.run_agent("第一句", client=client, verbose=False)
        mini_agent.run_agent("第二句", client=client, verbose=False)
        sent = client.messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user"])
        self.assertEqual(sent[0]["content"], "第二句")

    def test_failed_turn_still_leaves_valid_alternation(self) -> None:
        """调模型失败也要补一条 assistant，否则下一轮会连续两条 user。"""

        class Boom:
            class messages:  # noqa: N801 - 模拟 SDK 结构
                @staticmethod
                def create(**_: Any) -> Any:
                    raise RuntimeError("boom")

        history: List[Dict[str, Any]] = []
        mini_agent.run_agent("x", client=Boom(), verbose=False, history=history)
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])

    def test_interactive_repl_carries_context(self) -> None:
        client = FakeClient(
            [
                FakeResponse([text_block("你好呀")], "end_turn"),
                FakeResponse([text_block("记得")], "end_turn"),
            ]
        )
        with mock.patch("builtins.input", side_effect=["你好", "还记得吗", "/exit"]):
            with redirect_stdout(io.StringIO()) as out:
                mini_agent.interactive(client=client, verbose=False)

        self.assertIn("你好呀", out.getvalue())
        sent = client.messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user", "assistant", "user"])

    def test_piped_input_surrogates_are_repaired(self) -> None:
        """管道里的 UTF-8 被按 GBK 解码后留下的代理字符要还原成正常中文。"""
        mojibake = "中文测试".encode("utf-8").decode("gbk", "surrogateescape")
        self.assertNotEqual(mojibake, "中文测试")
        self.assertEqual(mini_agent._clean_input(mojibake), "中文测试")
        self.assertEqual(mini_agent._clean_input("正常文本"), "正常文本")

    def test_interactive_clear_resets_context(self) -> None:
        client = FakeClient(
            [
                FakeResponse([text_block("旧话题")], "end_turn"),
                FakeResponse([text_block("新话题")], "end_turn"),
            ]
        )
        with mock.patch("builtins.input", side_effect=["旧话题", "/clear", "新问题", "/exit"]):
            with redirect_stdout(io.StringIO()):
                mini_agent.interactive(client=client, verbose=False)

        sent = client.messages.calls[1]["messages"]
        self.assertEqual([m["role"] for m in sent], ["user"])
        self.assertEqual(sent[0]["content"], "新问题")


def _run_with_max_loop(client: FakeClient, max_loop: int) -> str:
    """run_agent 沿用文档的全局 MAX_LOOP，测试里临时改它来限制循环。"""
    original = mini_agent.MAX_LOOP
    mini_agent.MAX_LOOP = max_loop
    try:
        return mini_agent.run_agent("x", client=client, verbose=False)
    finally:
        mini_agent.MAX_LOOP = original


if __name__ == "__main__":
    unittest.main(verbosity=2)

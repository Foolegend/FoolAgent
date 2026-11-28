"""流式输出测试：SSE 解析、增量拼装、事件契约（不联网）。"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402


# ── 构造原始流事件 ──────────────────────────────────────────

def text_events(text: str, chunk_size: int = 4) -> List[Dict[str, Any]]:
    """一段纯文本回复的流式事件。"""
    events: List[Dict[str, Any]] = [
        {"type": "message_start", "message": {"stop_reason": None, "usage": {}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ]
    for index in range(0, len(text), chunk_size):
        events.append({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text[index:index + chunk_size]},
        })
    events.append({"type": "content_block_stop", "index": 0})
    events.append({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    events.append({"type": "message_stop"})
    return events


def tool_events(
    name: str, args: Dict[str, Any], block_id: str = "call_1"
) -> List[Dict[str, Any]]:
    """一段「说了句话 + 调工具」的流式事件，参数故意切成碎片。"""
    events: List[Dict[str, Any]] = [
        {"type": "message_start", "message": {"stop_reason": None}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "我先看看。"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": block_id,
                "name": name,
                "input": {},
            },
        },
    ]
    raw = json.dumps(args, ensure_ascii=False)
    for index in range(0, len(raw), 5):
        events.append({
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": raw[index:index + 5]},
        })
    events.append({"type": "content_block_stop", "index": 1})
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
    events.append({"type": "message_stop"})
    return events


class FakeStreamMessages:
    """支持 stream=True 的假 messages 资源。"""

    def __init__(self, scripts: List[List[Dict[str, Any]]]):
        self._scripts = list(scripts)
        self.calls: List[Dict[str, Any]] = []

    def create(self, *, stream: bool = False, **kwargs: Any):
        self.calls.append(dict(kwargs, stream=stream))
        if not self._scripts:
            raise AssertionError("假 client 的响应已用完")
        script = self._scripts.pop(0)
        if stream:
            return iter(script)
        # 非流式分支：把流事件还原成一条完整响应
        assembler = mini_agent._StreamAssembler()
        for event in script:
            assembler.feed(event)
        return assembler.response()


class FakeStreamClient:
    def __init__(self, scripts: List[List[Dict[str, Any]]]):
        self.messages = FakeStreamMessages(scripts)


class FakePlainMessages:
    """不支持流式的假 messages 资源：create 没有 stream 参数。"""

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakePlainClient:
    def __init__(self, responses: List[Any]):
        self.messages = FakePlainMessages(responses)


class FakeResponse:
    def __init__(self, content: List[Dict[str, Any]], stop_reason: str):
        self.content = content
        self.stop_reason = stop_reason


class FakeHTTPResponse:
    """假的 HTTP 响应，支持按行迭代（urllib 就是这么读 SSE 的）。"""

    def __init__(self, body: bytes):
        self._body = body
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        for line in self._body.splitlines(keepends=True):
            yield line

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


# ── SSE 解析 ───────────────────────────────────────────────

class SseParsingTest(unittest.TestCase):
    def test_parses_events_split_by_blank_lines(self) -> None:
        body = (
            b'event: message_start\n'
            b'data: {"type": "message_start", "message": {}}\n'
            b'\n'
            b'event: content_block_delta\n'
            b'data: {"type": "content_block_delta", "index": 0}\n'
            b'\n'
        )
        events = list(mini_agent._iter_sse(FakeHTTPResponse(body)))
        self.assertEqual([e["type"] for e in events],
                         ["message_start", "content_block_delta"])

    def test_skips_heartbeat_and_done_marker(self) -> None:
        body = (
            b': keep-alive\n'
            b'\n'
            b'data: {"type": "content_block_delta"}\n'
            b'\n'
            b'data: [DONE]\n'
            b'\n'
        )
        events = list(mini_agent._iter_sse(FakeHTTPResponse(body)))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "content_block_delta")

    def test_handles_event_without_trailing_blank_line(self) -> None:
        body = b'data: {"type": "message_stop"}'
        events = list(mini_agent._iter_sse(FakeHTTPResponse(body)))
        self.assertEqual(events[0]["type"], "message_stop")

    def test_compat_client_requests_stream(self) -> None:
        body = b'data: {"type": "message_stop"}\n\n'
        response = FakeHTTPResponse(body)
        with mock.patch.object(
            mini_agent.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            client = mini_agent.AnthropicCompatClient("tok", "http://example.test")
            events = list(
                client.create(stream=True, model="m", messages=[{"role": "user", "content": "hi"}])
            )

        self.assertEqual(events[0]["type"], "message_stop")
        request = urlopen.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], "m")
        self.assertTrue(response.closed)

    def test_compat_client_reports_unsupported_stream(self) -> None:
        """服务端拒绝 stream 参数时，退回一次性调用而不是整轮失败。"""
        error = urllib.error.HTTPError(
            "http://example.test", 400, "Bad Request", {},
            io.BytesIO(b"stream not supported"),
        )
        with mock.patch.object(
            mini_agent.urllib.request, "urlopen", side_effect=error
        ):
            client = mini_agent.AnthropicCompatClient("tok", "http://example.test")
            with self.assertRaises(mini_agent._StreamingUnsupported):
                list(client.create(stream=True, model="m", messages=[]))


# ── 增量拼装 ───────────────────────────────────────────────

class AssemblerTest(unittest.TestCase):
    def test_assembles_text_and_stop_reason(self) -> None:
        assembler = mini_agent._StreamAssembler()
        for event in text_events("你好世界"):
            assembler.feed(event)
        response = assembler.response()
        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(response.content[0]["text"], "你好世界")

    def test_assembles_fragmented_tool_input(self) -> None:
        assembler = mini_agent._StreamAssembler()
        for event in tool_events("bash", {"command": "ls -la"}):
            assembler.feed(event)
        response = assembler.response()
        self.assertEqual(response.stop_reason, "tool_use")
        tool_block = response.content[1]
        self.assertEqual(tool_block["type"], "tool_use")
        self.assertEqual(tool_block["name"], "bash")
        self.assertEqual(tool_block["input"], {"command": "ls -la"})

    def test_reports_text_and_thinking_deltas(self) -> None:
        assembler = mini_agent._StreamAssembler()
        deltas = []
        for event in [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "在想"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "答案"}},
        ]:
            delta = assembler.feed(event)
            if delta:
                deltas.append(delta)
        self.assertEqual(deltas, [("thinking", "在想"), ("text", "答案")])


# ── agent loop 里的流式行为 ────────────────────────────────

class StreamingAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

    def _restore_workspace(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace

    def test_text_deltas_are_emitted_in_order(self) -> None:
        client = FakeStreamClient([text_events("你好世界")])
        events: List[Dict[str, Any]] = []
        result = mini_agent.run_agent(
            "hi", client=client, verbose=False, on_event=events.append
        )

        self.assertEqual(result, "你好世界")
        deltas = [e["text"] for e in events if e["type"] == "text_delta"]
        self.assertEqual("".join(deltas), "你好世界")
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["text"], "你好世界")

    def test_on_text_receives_streamed_chunks(self) -> None:
        client = FakeStreamClient([text_events("边生成边显示")])
        chunks: List[str] = []
        mini_agent.run_agent(
            "hi", client=client, verbose=False, on_text=chunks.append
        )
        self.assertEqual("".join(chunks), "边生成边显示")

    def test_tool_call_from_stream_is_executed(self) -> None:
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        client = FakeStreamClient([
            tool_events("read", {"path": "a.txt"}),
            text_events("读完了"),
        ])
        events: List[Dict[str, Any]] = []
        result = mini_agent.run_agent(
            "读文件", client=client, verbose=False, on_event=events.append
        )

        self.assertEqual(result, "读完了")
        types = [e["type"] for e in events]
        self.assertIn("tool_call", types)
        self.assertIn("tool_result", types)

        call = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(call["name"], "read")
        self.assertEqual(call["input"], {"path": "a.txt"})

        # 工具参数切片后要能拼回完整的 dict，并原样发给模型
        second_call = client.messages.calls[1]
        assistant = second_call["messages"][1]
        tool_block = assistant["content"][1]
        self.assertEqual(tool_block["type"], "tool_use")
        self.assertEqual(tool_block["input"], {"path": "a.txt"})

    def test_streamed_text_is_not_emitted_twice(self) -> None:
        """流式模式下过渡性文字走 text_delta，不再额外发 assistant_text。"""
        client = FakeStreamClient([
            tool_events("read", {"path": "a.txt"}),
            text_events("好了"),
        ])
        events: List[Dict[str, Any]] = []
        mini_agent.run_agent("x", client=client, verbose=False, on_event=events.append)
        types = [e["type"] for e in events]
        self.assertNotIn("assistant_text", types)
        self.assertIn("text_delta", types)

    def test_plain_client_keeps_old_behaviour(self) -> None:
        """不支持流式的客户端：没有 text_delta，过渡性文字仍走 assistant_text。"""
        client = FakePlainClient([
            FakeResponse(
                [
                    {"type": "text", "text": "我先看看。"},
                    {"type": "tool_use", "id": "call_1", "name": "read",
                     "input": {"path": "a.txt"}},
                ],
                "tool_use",
            ),
            FakeResponse([{"type": "text", "text": "好了"}], "end_turn"),
        ])
        events: List[Dict[str, Any]] = []
        mini_agent.run_agent("x", client=client, verbose=False, on_event=events.append)
        types = [e["type"] for e in events]
        self.assertNotIn("text_delta", types)
        self.assertIn("assistant_text", types)

    def test_plain_client_still_streams_text_to_terminal(self) -> None:
        """退回一次性调用时，文字照样要打到终端（整块）。"""
        client = FakePlainClient([
            FakeResponse([{"type": "text", "text": "一次性返回"}], "end_turn"),
        ])
        chunks: List[str] = []
        mini_agent.run_agent("x", client=client, verbose=False, on_text=chunks.append)
        self.assertEqual("".join(chunks), "一次性返回")

    def test_stream_failure_falls_back_to_single_call(self) -> None:
        """服务端不收 stream 参数时自动退回一次性调用，不能整轮失败。"""

        class PickyStream(FakeStreamMessages):
            def create(self, *, stream: bool = False, **kwargs: Any):
                if stream:
                    raise mini_agent._StreamingUnsupported("不支持流式")
                return super().create(stream=False, **kwargs)

        class PickyClient:
            def __init__(self) -> None:
                self.messages = PickyStream([])

        client = PickyClient()
        client.messages._scripts = [text_events("退回一次性")]
        chunks: List[str] = []
        result = mini_agent.run_agent(
            "hi", client=client, verbose=False, on_text=chunks.append
        )
        self.assertEqual(result, "退回一次性")
        self.assertEqual("".join(chunks), "退回一次性")

    def test_empty_stream_falls_back_to_single_call(self) -> None:
        """流式一个事件都没回来（网关返回空体）时退回一次性调用。"""

        class EmptyStream(FakeStreamMessages):
            def create(self, *, stream: bool = False, **kwargs: Any):
                if stream:
                    self.calls.append(dict(kwargs, stream=True))
                    return iter([])
                return FakeResponse([{"type": "text", "text": "一次性结果"}], "end_turn")

        class EmptyClient:
            def __init__(self) -> None:
                self.messages = EmptyStream([])

        client = EmptyClient()
        chunks: List[str] = []
        result = mini_agent.run_agent(
            "hi", client=client, verbose=False, on_text=chunks.append
        )
        self.assertEqual(result, "一次性结果")
        self.assertEqual("".join(chunks), "一次性结果")


if __name__ == "__main__":
    unittest.main(verbosity=2)

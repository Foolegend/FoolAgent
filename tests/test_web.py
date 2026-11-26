"""Web 对话框测试：agent 进度事件 + HTTP/SSE 接口（不联网）。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402
import web_server  # noqa: E402


class FakeResponse:
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
            raise AssertionError("假 client 的响应已用完")
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: List[FakeResponse]):
        self.messages = FakeMessages(responses)


def tool_use_block(name: str, arguments: Dict[str, Any], block_id: str = "call_1") -> Dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def event_types(events: List[Dict[str, Any]]) -> List[str]:
    return [event["type"] for event in events]


class AgentEventTest(unittest.TestCase):
    """run_agent(on_event=...) 的事件契约。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

    def _restore_workspace(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace

    def test_emits_tool_call_and_final_events(self) -> None:
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        client = FakeClient(
            [
                FakeResponse([tool_use_block("read", {"path": "a.txt"})], "tool_use"),
                FakeResponse([text_block("读完了")], "end_turn"),
            ]
        )
        events: List[Dict[str, Any]] = []
        result = mini_agent.run_agent(
            "读文件", client=client, verbose=False, on_event=events.append
        )

        self.assertEqual(result, "读完了")
        self.assertEqual(
            event_types(events),
            ["step", "tool_call", "tool_result", "step", "final"],
        )

        call = events[1]
        self.assertEqual(call["name"], "read")
        self.assertEqual(call["input"], {"path": "a.txt"})
        self.assertEqual(events[2]["id"], call["id"])
        self.assertIn("hello", events[2]["output"])
        self.assertEqual(events[-1]["text"], "读完了")

    def test_emits_error_event_when_model_fails(self) -> None:
        class Boom:
            class messages:  # noqa: N801 - 模拟 SDK 结构
                @staticmethod
                def create(**_: Any) -> Any:
                    raise RuntimeError("401 Unauthorized")

        events: List[Dict[str, Any]] = []
        mini_agent.run_agent("x", client=Boom(), verbose=False, on_event=events.append)
        self.assertEqual(event_types(events), ["step", "error"])
        self.assertIn("401", events[-1]["message"])

    def test_callback_exception_propagates(self) -> None:
        """Web 端靠回调抛异常感知浏览器断开，不能被吞掉。"""

        class Disconnected(Exception):
            pass

        client = FakeClient([FakeResponse([text_block("ok")], "end_turn")])

        def boom(_event):
            raise Disconnected()

        with self.assertRaises(Disconnected):
            mini_agent.run_agent("x", client=client, verbose=False, on_event=boom)


class WebServerTest(unittest.TestCase):
    """真实起一个本地 HTTP 服务，跑通页面、SSE 和多轮上下文。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

        (self.root / "a.txt").write_text("hello-web\n", encoding="utf-8")
        self.client = FakeClient(
            [
                FakeResponse([tool_use_block("read", {"path": "a.txt"})], "tool_use"),
                FakeResponse([text_block("第一轮完成")], "end_turn"),
                FakeResponse([text_block("第二轮完成")], "end_turn"),
                FakeResponse([text_block("重置后完成")], "end_turn"),
            ]
        )
        self.server = web_server.create_server("127.0.0.1", 0, client=self.client)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._stop_server)

    def _restore_workspace(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # ── HTTP 辅助 ─────────────────────────────────────────

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.headers.get_content_type(), response.read()

    def post_json(self, path: str, payload: Dict[str, Any]):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def stream_chat(self, message: str, session_id: str = "") -> List[Dict[str, Any]]:
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"message": message, "session_id": session_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        events: List[Dict[str, Any]] = []
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertTrue(
                response.headers.get_content_type().startswith("text/event-stream")
            )
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        return events

    # ── 用例 ─────────────────────────────────────────────

    def test_static_page_and_info(self) -> None:
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("拙愚智能".encode("utf-8"), body)
        self.assertIn(b"/yugong.svg", body)

        status, content_type, body = self.get("/yugong.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", content_type)
        self.assertIn(b"<svg", body)

        status, content_type, body = self.get("/yugong-light.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", content_type)
        self.assertIn(b"<svg", body)

        status, content_type, body = self.get("/favicon.png")
        self.assertEqual(status, 200)
        self.assertIn("image/png", content_type)
        self.assertTrue(body.startswith(b"\x89PNG"))

        status, content_type, body = self.get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertIn("image/x-icon", content_type)

        status, content_type, body = self.get("/markdown.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", content_type)
        self.assertIn(b"renderMarkdown", body)

        status, content_type, body = self.get("/api/info")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        info = json.loads(body)
        self.assertIn("model", info)
        self.assertIn("workspace", info)

    def test_chat_streams_every_step(self) -> None:
        events = self.stream_chat("读文件", session_id="sse-demo")

        self.assertEqual(
            event_types(events),
            ["session", "step", "tool_call", "tool_result", "step", "final", "done"],
        )
        self.assertEqual(events[0]["session_id"], "sse-demo")
        self.assertEqual(events[2]["name"], "read")
        self.assertIn("hello-web", events[3]["output"])
        self.assertEqual(events[5]["text"], "第一轮完成")
        self.assertEqual(events[6]["text"], "第一轮完成")

    def test_session_history_is_reused(self) -> None:
        self.stream_chat("第一问", session_id="multi")
        events = self.stream_chat("第二问", session_id="multi")
        self.assertEqual(events[-1]["text"], "第二轮完成")

        second_call = self.client.messages.calls[-1]
        roles = [message["role"] for message in second_call["messages"]]
        # 第一轮 = user + assistant(tool_use) + user(tool_result) + assistant(text)
        self.assertEqual(roles, ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(second_call["messages"][-1]["content"], "第二问")

    def test_reset_clears_history(self) -> None:
        self.stream_chat("先聊一句", session_id="reset-me")
        status, payload = self.post_json("/api/reset", {"session_id": "reset-me"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        self.stream_chat("重置后", session_id="reset-me")
        third_call = self.client.messages.calls[-1]
        self.assertEqual([m["role"] for m in third_call["messages"]], ["user"])
        self.assertEqual(third_call["messages"][0]["content"], "重置后")

    def test_empty_message_is_rejected(self) -> None:
        status, payload = self.post_json("/api/chat", {"message": "   "})
        self.assertEqual(status, 400)
        self.assertIn("不能为空", payload["error"])

    def test_unknown_path_returns_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/no-such-page")
        try:
            self.assertEqual(caught.exception.code, 404)
        finally:
            caught.exception.close()

    def test_cross_origin_post_is_rejected(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"message": "hi"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        try:
            self.assertEqual(caught.exception.code, 403)
        finally:
            caught.exception.close()

    def test_non_json_post_is_rejected(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=b"message=hi",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        try:
            self.assertEqual(caught.exception.code, 415)
        finally:
            caught.exception.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

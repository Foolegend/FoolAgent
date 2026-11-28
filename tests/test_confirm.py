"""操作确认测试：写文件 / 改文件 / 执行命令之前要问过用户（不联网）。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

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
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: List[FakeResponse]):
        self.messages = FakeMessages(responses)


def tool_use(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "tool_use", "id": "call_1", "name": name, "input": args}


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


class ConfirmCoreTest(unittest.TestCase):
    """run_agent(on_confirm=...) 的确认逻辑。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

    def _restore_workspace(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace

    def test_write_is_not_executed_when_denied(self) -> None:
        client = FakeClient([
            FakeResponse([tool_use("write", {"path": "x.txt", "content": "hi"})], "tool_use"),
            FakeResponse([text_block("那就不写了")], "end_turn"),
        ])
        events: List[Dict[str, Any]] = []
        mini_agent.run_agent(
            "写文件", client=client, verbose=False,
            on_event=events.append, on_confirm=lambda name, args: False,
        )

        self.assertFalse((self.root / "x.txt").exists())
        # 模型要收到"被拒绝"，才知道换个做法
        tool_result = client.messages.calls[1]["messages"][-1]["content"][0]
        self.assertIn("拒绝", tool_result["content"])
        self.assertIn("tool_denied", [e["type"] for e in events])

    def test_write_executes_when_approved(self) -> None:
        client = FakeClient([
            FakeResponse([tool_use("write", {"path": "x.txt", "content": "hi"})], "tool_use"),
            FakeResponse([text_block("写好了")], "end_turn"),
        ])
        confirmed: List[str] = []
        mini_agent.run_agent(
            "写文件", client=client, verbose=False,
            on_confirm=lambda name, args: confirmed.append(name) or True,
        )
        self.assertEqual(confirmed, ["write"])
        self.assertEqual((self.root / "x.txt").read_text(encoding="utf-8"), "hi")

    def test_read_does_not_ask_for_confirmation(self) -> None:
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        client = FakeClient([
            FakeResponse([tool_use("read", {"path": "a.txt"})], "tool_use"),
            FakeResponse([text_block("读完了")], "end_turn"),
        ])
        asked: List[str] = []
        mini_agent.run_agent(
            "读文件", client=client, verbose=False,
            on_confirm=lambda name, args: asked.append(name) or True,
        )
        self.assertEqual(asked, [])

    def test_bash_and_edit_also_ask(self) -> None:
        (self.root / "a.txt").write_text("old\n", encoding="utf-8")
        client = FakeClient([
            FakeResponse([tool_use("edit", {"path": "a.txt", "old_text": "old",
                                            "new_text": "new"})], "tool_use"),
            FakeResponse([tool_use("bash", {"command": "ls"})], "tool_use"),
            FakeResponse([text_block("好了")], "end_turn"),
        ])
        asked: List[str] = []
        mini_agent.run_agent(
            "改文件", client=client, verbose=False,
            on_confirm=lambda name, args: asked.append(name) or True,
        )
        self.assertEqual(asked, ["edit", "bash"])

    def test_confirmation_error_is_treated_as_denial(self) -> None:
        client = FakeClient([
            FakeResponse([tool_use("write", {"path": "x.txt", "content": "hi"})], "tool_use"),
            FakeResponse([text_block("好")], "end_turn"),
        ])

        def boom(name, args):
            raise RuntimeError("确认服务挂了")

        events: List[Dict[str, Any]] = []
        mini_agent.run_agent(
            "写文件", client=client, verbose=False,
            on_event=events.append, on_confirm=boom,
        )
        self.assertFalse((self.root / "x.txt").exists())
        self.assertIn("tool_denied", [e["type"] for e in events])

    def test_no_confirm_callback_means_no_confirmation(self) -> None:
        """不传 on_confirm 就照常执行（老行为不变）。"""
        client = FakeClient([
            FakeResponse([tool_use("write", {"path": "x.txt", "content": "hi"})], "tool_use"),
            FakeResponse([text_block("写好了")], "end_turn"),
        ])
        mini_agent.run_agent("写文件", client=client, verbose=False)
        self.assertTrue((self.root / "x.txt").exists())


class TerminalConfirmTest(unittest.TestCase):
    """终端里的确认提示。"""

    def test_yes_approves(self) -> None:
        confirmer = mini_agent._make_confirmer()
        with mock.patch("mini_agent._stdin_is_interactive", return_value=True):
            with mock.patch("builtins.input", return_value="y"):
                self.assertTrue(confirmer("bash", {"command": "ls"}))

    def test_anything_else_denies(self) -> None:
        confirmer = mini_agent._make_confirmer()
        with mock.patch("mini_agent._stdin_is_interactive", return_value=True):
            with mock.patch("builtins.input", return_value="n"):
                self.assertFalse(confirmer("bash", {"command": "ls"}))
            with mock.patch("builtins.input", return_value=""):
                self.assertFalse(confirmer("write", {"path": "a.txt"}))

    def test_eof_denies(self) -> None:
        confirmer = mini_agent._make_confirmer()
        with mock.patch("mini_agent._stdin_is_interactive", return_value=True):
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertFalse(confirmer("write", {"path": "a.txt"}))

    def test_auto_mode_skips_the_prompt(self) -> None:
        confirmer = mini_agent._make_confirmer(auto_yes=True)
        with mock.patch("mini_agent._stdin_is_interactive", return_value=True):
            with mock.patch("builtins.input", side_effect=AssertionError("不该问")):
                self.assertTrue(confirmer("bash", {"command": "ls"}))

    def test_non_interactive_terminal_auto_approves(self) -> None:
        """管道里没法问人，只能放行（并提示一次）。"""
        confirmer = mini_agent._make_confirmer()
        with mock.patch("mini_agent._stdin_is_interactive", return_value=False):
            with mock.patch("builtins.input", side_effect=AssertionError("不该问")):
                self.assertTrue(confirmer("bash", {"command": "ls"}))
        self.assertTrue(confirmer.state["warned"])

    def test_describe_tool_is_human_readable(self) -> None:
        self.assertIn("ls", mini_agent._describe_tool("bash", {"command": "ls"}))
        self.assertIn("a.txt", mini_agent._describe_tool("write", {"path": "a.txt"}))
        self.assertIn("a.txt", mini_agent._describe_tool("edit", {"path": "a.txt"}))


class WebConfirmTest(unittest.TestCase):
    """网页版：agent 问 → 前端答 → 继续执行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

        self.client = FakeClient([
            FakeResponse([tool_use("write", {"path": "x.txt", "content": "hi"})], "tool_use"),
            FakeResponse([text_block("写好了")], "end_turn"),
        ])
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

    def _post_json(self, path: str, payload: Dict[str, Any]):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_approve_lets_the_tool_run(self) -> None:
        events: List[Dict[str, Any]] = []
        answer: List[Dict[str, Any]] = []

        def run_chat():
            request = urllib.request.Request(
                self.base + "/api/chat",
                data=json.dumps({"message": "写文件"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:].strip()))

        worker = threading.Thread(target=run_chat, daemon=True)
        worker.start()

        # 等 agent 发出确认请求
        request_id = ""
        for _ in range(100):
            for event in list(events):
                if event.get("type") == "confirm":
                    request_id = event["id"]
            if request_id:
                break
            threading.Event().wait(0.05)
        self.assertTrue(request_id, "没有收到 confirm 事件")

        confirm_event = next(e for e in events if e["type"] == "confirm")
        self.assertEqual(confirm_event["name"], "write")
        self.assertEqual(confirm_event["input"]["path"], "x.txt")

        status, payload = self._post_json(
            "/api/confirm", {"request_id": request_id, "approved": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        worker.join(timeout=30)
        self.assertTrue((self.root / "x.txt").exists())
        self.assertIn("final", [e["type"] for e in events])

    def test_deny_blocks_the_tool(self) -> None:
        events: List[Dict[str, Any]] = []

        def run_chat():
            request = urllib.request.Request(
                self.base + "/api/chat",
                data=json.dumps({"message": "写文件"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:].strip()))

        worker = threading.Thread(target=run_chat, daemon=True)
        worker.start()

        request_id = ""
        for _ in range(100):
            for event in list(events):
                if event.get("type") == "confirm":
                    request_id = event["id"]
            if request_id:
                break
            threading.Event().wait(0.05)
        self.assertTrue(request_id)

        self._post_json("/api/confirm", {"request_id": request_id, "approved": False})
        worker.join(timeout=30)

        self.assertFalse((self.root / "x.txt").exists())
        self.assertIn("tool_denied", [e["type"] for e in events])

    def test_unknown_request_id_is_reported(self) -> None:
        status, payload = self._post_json(
            "/api/confirm", {"request_id": "does-not-exist", "approved": True}
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])

    def test_confirmation_can_be_disabled(self) -> None:
        server = web_server.create_server(
            "127.0.0.1", 0, client=self.client, require_confirm=False
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(
                base + "/api/chat",
                data=json.dumps({"message": "写文件"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            events: List[Dict[str, Any]] = []
            with urllib.request.urlopen(request, timeout=30) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:].strip()))
            self.assertNotIn("confirm", [e["type"] for e in events])
            self.assertTrue((self.root / "x.txt").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

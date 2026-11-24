"""模型配置（.env 解析 + 客户端构造）的测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402


class DotenvParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_file = Path(self._tmp.name) / ".env"

    def _write(self, content: str) -> Path:
        self.env_file.write_text(content, encoding="utf-8")
        return self.env_file

    def test_parses_key_value_pairs(self) -> None:
        path = self._write("A=1\nB=2\n")
        self.assertEqual(mini_agent._load_dotenv(path), {"A": "1", "B": "2"})

    def test_skips_comments_and_blank_lines(self) -> None:
        path = self._write("# comment\n\nA=1\n   \n# another\nB=2\n")
        self.assertEqual(mini_agent._load_dotenv(path), {"A": "1", "B": "2"})

    def test_strips_quotes_and_inline_comments(self) -> None:
        path = self._write('A="quoted value"\nB=plain # trailing comment\n')
        values = mini_agent._load_dotenv(path)
        self.assertEqual(values["A"], "quoted value")
        self.assertEqual(values["B"], "plain")

    def test_keeps_hash_inside_quotes(self) -> None:
        path = self._write('A="has # hash"\n')
        self.assertEqual(mini_agent._load_dotenv(path)["A"], "has # hash")

    def test_supports_export_prefix(self) -> None:
        path = self._write("export A=1\n")
        self.assertEqual(mini_agent._load_dotenv(path), {"A": "1"})

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(mini_agent._load_dotenv(Path("nope.env")), {})

    def test_none_path_returns_empty(self) -> None:
        self.assertEqual(mini_agent._load_dotenv(None), {})


class SettingPrecedenceTest(unittest.TestCase):
    def test_environment_variable_wins_over_dotenv(self) -> None:
        os.environ["MINI_AGENT_TEST_KEY"] = "from-env"
        self.addCleanup(os.environ.pop, "MINI_AGENT_TEST_KEY")
        original = mini_agent.DOTENV
        mini_agent.DOTENV = {"MINI_AGENT_TEST_KEY": "from-dotenv"}
        self.addCleanup(setattr, mini_agent, "DOTENV", original)
        self.assertEqual(mini_agent._setting("MINI_AGENT_TEST_KEY"), "from-env")

    def test_falls_back_to_dotenv(self) -> None:
        original = mini_agent.DOTENV
        mini_agent.DOTENV = {"MINI_AGENT_TEST_KEY": "from-dotenv"}
        self.addCleanup(setattr, mini_agent, "DOTENV", original)
        self.assertEqual(mini_agent._setting("MINI_AGENT_TEST_KEY"), "from-dotenv")

    def test_returns_default_when_absent(self) -> None:
        self.assertEqual(mini_agent._setting("MINI_AGENT_ABSENT_KEY", "fallback"), "fallback")


class ClientFactoryTest(unittest.TestCase):
    def test_builds_client_from_config(self) -> None:
        original_model = mini_agent.MODEL
        original_url = mini_agent.BASE_URL
        original_token = mini_agent.AUTH_TOKEN
        mini_agent.BASE_URL = "https://example.invalid/anthropic"
        mini_agent.AUTH_TOKEN = "token-for-test"

        def restore() -> None:
            mini_agent.MODEL = original_model
            mini_agent.BASE_URL = original_url
            mini_agent.AUTH_TOKEN = original_token

        self.addCleanup(restore)

        client = mini_agent._build_client()
        self.assertTrue(hasattr(client, "messages"))
        self.assertTrue(hasattr(client.messages, "create"))

    def test_missing_credentials_raise_clear_error(self) -> None:
        original_url = mini_agent.BASE_URL
        original_token = mini_agent.AUTH_TOKEN
        original_anthropic = mini_agent.Anthropic
        mini_agent.BASE_URL = ""
        mini_agent.AUTH_TOKEN = ""
        mini_agent.Anthropic = None  # 强制走标准库分支

        def restore() -> None:
            mini_agent.BASE_URL = original_url
            mini_agent.AUTH_TOKEN = original_token
            mini_agent.Anthropic = original_anthropic

        self.addCleanup(restore)

        with self.assertRaises(RuntimeError) as caught:
            mini_agent._build_client()
        self.assertIn("凭据", str(caught.exception))


class CompatClientTest(unittest.TestCase):
    def test_client_targets_messages_endpoint(self) -> None:
        client = mini_agent.AnthropicCompatClient("tok", "https://example.invalid/anthropic/")
        self.assertEqual(client.base_url, "https://example.invalid/anthropic")
        self.assertIs(client.messages, client)


if __name__ == "__main__":
    unittest.main(verbosity=2)

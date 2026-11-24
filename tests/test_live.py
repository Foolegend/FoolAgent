"""真实 API 集成测试（可选，需要凭据）。

本机没装 anthropic SDK、也没网络装，所以这里用标准库 urllib 实现一个
最小 Anthropic 兼容客户端，只为验证 run_agent 的请求/响应处理是否正确。

跳过条件：没有配置 INVEST_ANTHROPIC_AUTH_TOKEN（或没有 .env）。
用法：
    python -m unittest tests.test_live -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> Dict[str, str]:
    """读 .env，不覆盖已存在的环境变量。"""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


DOTENV = load_dotenv(REPO_ROOT / ".env")


def _setting(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name) or DOTENV.get(name) or default


TOKEN = _setting("INVEST_ANTHROPIC_AUTH_TOKEN")
BASE_URL = _setting("INVEST_ANTHROPIC_BASE_URL", "https://api.minimax.cn/anthropic")
MODEL = _setting("INVEST_ANTHROPIC_MODEL", "MiniMax-M3.1-Flash")


@unittest.skipUnless(TOKEN, "未配置 INVEST_ANTHROPIC_AUTH_TOKEN，跳过真实 API 测试")
class LiveApiTest(unittest.TestCase):
    """真跑一次，确认请求格式和响应处理都对。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        self._original_model = mini_agent.MODEL
        mini_agent.WORKSPACE = self.root
        mini_agent.MODEL = MODEL
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace
        mini_agent.MODEL = self._original_model

    def test_agent_writes_a_file_and_verifies_it(self) -> None:
        client = mini_agent.AnthropicCompatClient(TOKEN, BASE_URL)
        result = mini_agent.run_agent(
            "写一个 hello.py，内容是 print('hello-from-agent')，"
            "然后运行 python hello.py 确认输出正确，最后告诉我结果。",
            client=client,
            verbose=False,
        )
        script = self.root / "hello.py"
        self.assertTrue(script.exists(), f"agent 没有创建文件，最终回复：{result}")
        self.assertIn("hello-from-agent", script.read_text(encoding="utf-8"))
        self.assertNotIn("调用模型失败", result)
        self.assertTrue(result.strip(), "agent 没有给出最终回复")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Markdown 渲染器测试：直接调用 web/markdown.js（需要 node，没有则跳过）。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_JS = ROOT / "web" / "markdown.js"
NODE = shutil.which("node")

SCRIPT = (
    "const {renderMarkdown} = require(process.argv[1]);"
    "process.stdout.write(renderMarkdown(process.argv[2]));"
)


@unittest.skipUnless(NODE and MARKDOWN_JS.is_file(), "需要 node 和 web/markdown.js")
class MarkdownRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 快速验证 node 能加载模块，失败时整类跳过而不是报一堆错
        probe = subprocess.run(
            [NODE, "-e", SCRIPT, str(MARKDOWN_JS), "hello"],
            capture_output=True,
            timeout=20,
        )
        if probe.returncode != 0:
            raise unittest.SkipTest(probe.stderr.decode("utf-8", "replace"))

    def render(self, source: str) -> str:
        result = subprocess.run(
            [NODE, "-e", SCRIPT, str(MARKDOWN_JS), source],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stderr.decode("utf-8", "replace"),
        )
        return result.stdout.decode("utf-8")

    def test_headings(self) -> None:
        html = self.render("# 一级标题\n\n### 三级标题")
        self.assertIn("<h1>一级标题</h1>", html)
        self.assertIn("<h3>三级标题</h3>", html)

    def test_inline_formatting(self) -> None:
        html = self.render("**粗体** 和 *斜体* 和 ~~删除~~ 和 `code`")
        self.assertIn("<strong>粗体</strong>", html)
        self.assertIn("<em>斜体</em>", html)
        self.assertIn("<del>删除</del>", html)
        self.assertIn("<code>code</code>", html)

    def test_soft_line_break(self) -> None:
        html = self.render("第一行\n第二行")
        self.assertIn("<p>第一行<br>第二行</p>", html)

    def test_fenced_code_is_escaped(self) -> None:
        html = self.render('```python\nprint("<script>x</script>")\n```')
        self.assertIn('class="codeblock"', html)
        self.assertIn('class="codelang">python', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_unordered_and_nested_list(self) -> None:
        html = self.render("- 第一项\n- 第二项\n  - 嵌套项")
        self.assertIn("<ul>", html)
        self.assertIn("<li>第一项</li>", html)
        self.assertIn("嵌套项", html)
        self.assertGreaterEqual(html.count("<ul>"), 2)

    def test_ordered_list_and_start_number(self) -> None:
        html = self.render("3. 第三\n4. 第四")
        self.assertIn('<ol start="3">', html)
        self.assertIn("<li>第三</li>", html)

    def test_task_list(self) -> None:
        html = self.render("- [x] 已完成\n- [ ] 未完成")
        self.assertIn('class="task"', html)
        self.assertIn("checked", html)
        self.assertNotIn("[x]", html)

    def test_table_alignment_and_inline_code(self) -> None:
        html = self.render(
            "| 名称 | 数量 |\n|:-----|-----:|\n| apple | `a|b` |"
        )
        self.assertIn("<table>", html)
        self.assertIn('style="text-align:left"', html)
        self.assertIn('style="text-align:right"', html)
        self.assertIn("<td style=\"text-align:right\"><code>a|b</code></td>", html)

    def test_blockquote_and_hr(self) -> None:
        html = self.render("> 引用一段\n\n---")
        self.assertIn("<blockquote><p>引用一段</p></blockquote>", html)
        self.assertIn("<hr>", html)

    def test_raw_html_is_escaped(self) -> None:
        html = self.render('<img src=x onerror=alert(1)>')
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img src=x", html)

    def test_javascript_link_is_not_rendered(self) -> None:
        html = self.render("[点我](javascript:alert(1))")
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<a ", html)
        self.assertIn("点我", html)

    def test_safe_link_is_rendered(self) -> None:
        html = self.render("[示例](https://example.com/a?b=1&c=2)")
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_backslash_escapes(self) -> None:
        html = self.render(r"\*不是斜体\*")
        self.assertNotIn("<em>", html)
        self.assertIn("*不是斜体*", html)

    def test_code_span_protects_underscores(self) -> None:
        html = self.render("`foo_bar_baz`")
        self.assertIn("<code>foo_bar_baz</code>", html)
        self.assertNotIn("<em>", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)

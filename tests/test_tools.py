"""四个原子工具的行为测试。

用标准库 unittest，无需额外依赖（也不需要 API key）。
测试通过 patch mini_agent.WORKSPACE 指向临时目录，工具函数签名保持文档原样。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mini_agent  # noqa: E402


class SandboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._original_workspace = mini_agent.WORKSPACE
        mini_agent.WORKSPACE = self.root
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_workspace)

    def _restore_workspace(self) -> None:
        mini_agent.WORKSPACE = self._original_workspace


class ReadToolTest(SandboxTestCase):
    def test_read_returns_numbered_lines(self) -> None:
        (self.root / "a.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = mini_agent.tool_read("a.txt")
        self.assertEqual(result, "1\tline1\n2\tline2\n3\tline3")

    def test_read_paginates_with_offset_and_limit(self) -> None:
        (self.root / "a.txt").write_text(
            "\n".join(f"l{i}" for i in range(1, 11)), encoding="utf-8"
        )
        result = mini_agent.tool_read("a.txt", offset=2, limit=3)
        self.assertEqual(result, "3\tl3\n4\tl4\n5\tl5")

    def test_read_missing_file_returns_error_string(self) -> None:
        result = mini_agent.tool_read("nope.txt")
        self.assertTrue(result.startswith("错误："), result)

    def test_read_rejects_path_escape(self) -> None:
        result = mini_agent.tool_read("../outside.txt")
        self.assertIn("路径越界", result)

    def test_read_rejects_absolute_path_outside_root(self) -> None:
        result = mini_agent.tool_read(str(Path(tempfile.gettempdir()) / "x.txt"))
        self.assertIn("路径越界", result)

    def test_read_rejects_sibling_prefix_directory(self) -> None:
        """C:\\workspace2 不应被当成 C:\\workspace 的子目录。"""
        sibling = self.root.parent / (self.root.name + "_evil")
        sibling.mkdir(exist_ok=True)
        self.addCleanup(sibling.rmdir)
        result = mini_agent.tool_read(str(sibling / "x.txt"))
        self.assertIn("路径越界", result)

    def test_read_rejects_directory(self) -> None:
        (self.root / "sub").mkdir()
        result = mini_agent.tool_read("sub")
        self.assertIn("是目录", result)

    def test_read_non_utf8_file_returns_error(self) -> None:
        (self.root / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
        result = mini_agent.tool_read("bin.dat")
        self.assertIn("UTF-8", result)


class WriteToolTest(SandboxTestCase):
    def test_write_creates_file_and_parent_dirs(self) -> None:
        result = mini_agent.tool_write("nested/deep/new.txt", "hello")
        self.assertIn("已写入", result)
        self.assertEqual(
            (self.root / "nested/deep/new.txt").read_text(encoding="utf-8"), "hello"
        )

    def test_write_overwrites_existing_file(self) -> None:
        (self.root / "a.txt").write_text("old", encoding="utf-8")
        mini_agent.tool_write("a.txt", "new")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "new")

    def test_write_rejects_path_escape(self) -> None:
        result = mini_agent.tool_write("../escaped.txt", "bad")
        self.assertIn("路径越界", result)
        self.assertFalse((self.root.parent / "escaped.txt").exists())


class EditToolTest(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    def test_edit_replaces_unique_text(self) -> None:
        result = mini_agent.tool_edit("a.txt", "beta", "BETA")
        self.assertIn("已编辑", result)
        self.assertEqual(
            (self.root / "a.txt").read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n"
        )

    def test_edit_rejects_non_unique_text(self) -> None:
        (self.root / "dup.txt").write_text("x\nx\n", encoding="utf-8")
        result = mini_agent.tool_edit("dup.txt", "x", "y")
        self.assertIn("不唯一", result)
        self.assertEqual((self.root / "dup.txt").read_text(encoding="utf-8"), "x\nx\n")

    def test_edit_missing_text_returns_guidance(self) -> None:
        result = mini_agent.tool_edit("a.txt", "zzz", "y")
        self.assertIn("未找到待替换文本", result)
        self.assertIn("read", result)

    def test_edit_rejects_path_escape(self) -> None:
        result = mini_agent.tool_edit("../outside.txt", "a", "b")
        self.assertIn("路径越界", result)


class BashToolTest(SandboxTestCase):
    def test_bash_runs_whitelisted_command(self) -> None:
        result = mini_agent.tool_bash("echo hello")
        self.assertIn("hello", result)

    def test_bash_rejects_non_whitelisted_command(self) -> None:
        result = mini_agent.tool_bash("rm -rf /")
        self.assertIn("不在白名单内", result)

    def test_bash_rejects_chained_non_whitelisted_command(self) -> None:
        """白名单必须逐段校验，否则 `echo ok && rm -rf /` 会漏过去。"""
        result = mini_agent.tool_bash("echo ok && rm -rf /")
        self.assertIn("不在白名单内", result)
        self.assertNotIn("ok", result)

    def test_bash_rejects_piped_non_whitelisted_command(self) -> None:
        result = mini_agent.tool_bash("echo hi | curl http://evil")
        self.assertIn("不在白名单内", result)

    def test_bash_allows_chained_whitelisted_commands(self) -> None:
        result = mini_agent.tool_bash("echo one && echo two")
        self.assertIn("one", result)
        self.assertIn("two", result)

    def test_bash_rejects_inline_code_execution(self) -> None:
        result = mini_agent.tool_bash('python -c "import os"')
        self.assertIn("内联执行代码", result)

    def test_bash_allows_running_a_script_file(self) -> None:
        (self.root / "s.py").write_text("print('from-script')", encoding="utf-8")
        result = mini_agent.tool_bash("python s.py")
        self.assertIn("from-script", result)

    def test_bash_reports_exit_code_and_stderr(self) -> None:
        (self.root / "fail.py").write_text(
            "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n", encoding="utf-8"
        )
        result = mini_agent.tool_bash("python fail.py")
        self.assertIn("boom", result)
        self.assertIn("[exit code] 3", result)

    def test_bash_truncates_long_output(self) -> None:
        (self.root / "big.py").write_text("print('x' * 50000)", encoding="utf-8")
        result = mini_agent.tool_bash("python big.py")
        self.assertIn("输出截断", result)
        self.assertLess(len(result), 30000)

    def test_bash_timeout_returns_error(self) -> None:
        (self.root / "slow.py").write_text(
            "import time\ntime.sleep(30)\n", encoding="utf-8"
        )
        result = mini_agent.tool_bash("python slow.py", timeout=1)
        self.assertIn("超时", result)

    def test_bash_empty_command_returns_error(self) -> None:
        result = mini_agent.tool_bash("   ")
        self.assertIn("命令为空", result)

    def test_bash_rejects_command_substitution(self) -> None:
        """`echo $(rm -rf /)` 的真命令藏在替换里，必须拦下。"""
        result = mini_agent.tool_bash("echo $(rm -rf /)")
        self.assertIn("命令替换", result)

    def test_bash_rejects_backtick_substitution(self) -> None:
        result = mini_agent.tool_bash("echo `rm -rf /`")
        self.assertIn("命令替换", result)

    def test_bash_rejects_absolute_path_argument(self) -> None:
        result = mini_agent.tool_bash("cat /etc/passwd")
        self.assertIn("路径越界", result)

    def test_bash_rejects_parent_directory_argument(self) -> None:
        result = mini_agent.tool_bash("cat ../secret.txt")
        self.assertIn("路径越界", result)

    def test_bash_rejects_tilde_expansion(self) -> None:
        result = mini_agent.tool_bash("cat ~/secret.txt")
        self.assertIn("超出沙箱范围", result)

    def test_bash_rejects_env_var_expansion_argument(self) -> None:
        result = mini_agent.tool_bash("cat %USERPROFILE%/secret.txt")
        self.assertIn("环境变量展开", result)

    def test_bash_rejects_path_hidden_in_option_value(self) -> None:
        """路径藏在等号后面也要拦：--output=/tmp/x、--git-dir=/tmp/y。"""
        outside = str(Path(tempfile.gettempdir()) / "evil.txt")
        for command in (
            f"git log --output={outside} -1",
            f"git --git-dir={outside} status",
            f"git log --pretty=format:{outside} -1",
        ):
            result = mini_agent.tool_bash(command)
            self.assertIn("路径越界", result, command)

    def test_bash_allows_ordinary_option_values(self) -> None:
        """别把正常选项值误判成路径。"""
        for command in (
            "git log --pretty=format:%h -1",
            "ls --color=always .",
            "git log --max-count=1",
        ):
            result = mini_agent.tool_bash(command)
            self.assertNotIn("路径越界", result, command)

    def test_bash_allows_relative_path_inside_sandbox(self) -> None:
        (self.root / "ok.txt").write_text("fine", encoding="utf-8")
        result = mini_agent.tool_bash("cat ok.txt")
        self.assertIn("fine", result)

    def test_bash_path_check_applies_to_every_segment(self) -> None:
        result = mini_agent.tool_bash("echo ok && cat /etc/passwd")
        self.assertIn("路径越界", result)

    def test_bash_allows_pwd(self) -> None:
        """白名单比文档多了 pwd：模型反复用它确认位置，只读无害。"""
        result = mini_agent.tool_bash("pwd")
        self.assertFalse(result.startswith("错误："), result)
        self.assertTrue(result.strip())

    # ── 白名单参数越权（命令名在白名单里，参数却能拉起别的程序）──

    def test_bash_rejects_find_exec(self) -> None:
        """find -exec 能执行白名单外的程序，命令名合法也必须拦。"""
        result = mini_agent.tool_bash('find . -exec sh -c "rm -rf /" ";"')
        self.assertIn("执行白名单外的程序", result)

    def test_bash_rejects_find_delete(self) -> None:
        result = mini_agent.tool_bash("find . -delete")
        self.assertIn("执行白名单外的程序", result)

    def test_bash_rejects_git_config_alias_exec(self) -> None:
        """git -c 能定义 alias 执行任意命令。"""
        result = mini_agent.tool_bash("git -c alias.qa=!rm -rf / qa")
        self.assertIn("执行白名单外的程序", result)

    def test_bash_rejects_git_global_config(self) -> None:
        """git config --global 写到工作区之外。"""
        result = mini_agent.tool_bash("git config --global user.name x")
        self.assertIn("工作区之外", result)

    def test_bash_allows_git_commit_reuse_option(self) -> None:
        """git commit -c HEAD~1 是子命令自己的选项，不能当成 git -c 一刀切。"""
        result = mini_agent.tool_bash("git commit -c HEAD~1")
        self.assertNotIn("不允许", result)

    # ── 分隔符：cmd.exe 不认 `;` ──

    def test_bash_runs_semicolon_separated_commands(self) -> None:
        """cmd.exe 把 `;` 当普通字符，修复前文件名会变成 's.py;'。"""
        (self.root / "s.py").write_text("print('from-script')", encoding="utf-8")
        result = mini_agent.tool_bash("python s.py; echo done")
        self.assertIn("from-script", result)
        self.assertNotIn("错误", result)

    def test_translate_separators_converts_semicolon(self) -> None:
        translated = mini_agent._translate_separators("echo one; echo two")
        self.assertNotIn(";", translated)
        self.assertIn("&", translated)

    def test_translate_separators_keeps_quoted_semicolon(self) -> None:
        translated = mini_agent._translate_separators('echo "a;b"')
        self.assertIn('"a;b"', translated)

    def test_translate_separators_keeps_escaped_semicolon(self) -> None:
        translated = mini_agent._translate_separators("find . -exec cmd \\;")
        self.assertIn("\\;", translated)

    # ── 输出解码 ──

    def test_bash_decodes_utf8_output(self) -> None:
        """MSYS 工具按 UTF-8 输出，按本地代码页解会变乱码。"""
        (self.root / "cn.py").write_text("print('中文输出')\n", encoding="utf-8")
        result = mini_agent.tool_bash("python cn.py")
        self.assertIn("中文输出", result)

    def test_decode_output_prefers_utf8(self) -> None:
        self.assertEqual(mini_agent._decode_output("中文".encode("utf-8")), "中文")

    def test_decode_output_falls_back_to_local_code_page(self) -> None:
        """cmd 内建命令按 OEM 代码页（GBK）输出，要能退回本地编码。"""
        raw = "中文".encode("gbk")
        self.assertEqual(mini_agent._decode_output(raw, fallback="gbk"), "中文")

    def test_decode_output_handles_empty_and_garbage(self) -> None:
        self.assertEqual(mini_agent._decode_output(b""), "")
        self.assertIsInstance(mini_agent._decode_output(b"\xff\xfe\x00"), str)


class ToolRegistryTest(unittest.TestCase):
    def test_registry_exposes_exactly_four_tools(self) -> None:
        self.assertEqual(
            sorted(mini_agent.TOOL_IMPL), ["bash", "edit", "read", "write"]
        )

    def test_tool_schemas_match_implementations(self) -> None:
        self.assertEqual(
            sorted(schema["name"] for schema in mini_agent.TOOLS),
            ["bash", "edit", "read", "write"],
        )

    def test_every_schema_has_required_fields(self) -> None:
        for schema in mini_agent.TOOLS:
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertEqual(schema["input_schema"]["type"], "object")


if __name__ == "__main__":
    unittest.main(verbosity=2)

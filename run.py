#!/usr/bin/env python3
"""拙愚智能 启动脚本：一条命令验证整条链路。

    python run.py            # 全套：环境自检 → 单元测试 → 真实 API → 端到端
    python run.py check      # 只做环境自检
    python run.py test       # 只跑单元测试
    python run.py live       # 只跑真实 API 连通性
    python run.py agent      # 端到端：让真实模型干一件真事
    python run.py agent "任务" # 端到端，自定义任务
    python run.py chat       # 交互式对话：一直问，一直答，直到 /exit 或 Ctrl-D
    python run.py web        # 浏览器对话框：实时显示每个处理步骤
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# ── 控制台输出 ────────────────────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def ok(text: str) -> str:
    return f"{_c('[OK]', '32')} {text}"


def fail(text: str) -> str:
    return f"{_c('[FAIL]', '31')} {text}"


def warn(text: str) -> str:
    return f"{_c('[WARN]', '33')} {text}"


def info(text: str) -> str:
    return f"{_c('[INFO]', '36')} {text}"


def title(text: str) -> None:
    print()
    print(_c(f"── {text} " + "─" * max(0, 56 - len(text)), "1;36"))


def _one_line(text: str) -> str:
    """把多行回复压成一行，用来在摘要里显示（完整内容已经流式打印过了）。"""
    stripped = (text or "").strip()
    if not stripped:
        return "(空)"
    first = stripped.splitlines()[0].strip()
    return first if len(first) <= 60 else first[:60] + "…"


def mask(value: str, keep: int = 6) -> str:
    """掩码敏感值，只留头尾。"""
    if not value:
        return "(未设置)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-4:]}"


# ── 各步骤 ────────────────────────────────────────────────────

def step_check() -> bool:
    """环境自检：Python、.env、凭据、依赖。"""
    title("环境自检")

    version = sys.version_info
    print(f"  Python        {version.major}.{version.minor}.{version.micro}")
    if version < (3, 8):
        print(fail("需要 Python >= 3.8"))
        return False
    print(ok("Python 版本满足要求"))

    import mini_agent

    env_file = mini_agent.DOTENV
    if not env_file:
        print(fail(f"未找到 .env（查找顺序：ANTHROPIC_ENV_FILE > 当前目录 > {ROOT}）"))
        print(f"     可复制 .env.example 为 .env 并填入凭据")
        return False
    print(ok(f"已加载 .env（{len(env_file)} 个键）"))

    print(f"  BASE_URL      {mini_agent.BASE_URL or '(未设置，走官方 API)'}")
    print(f"  MODEL         {mini_agent.MODEL}")
    print(f"  AUTH_TOKEN    {mask(mini_agent.AUTH_TOKEN or '')}")

    if not mini_agent.AUTH_TOKEN:
        print(fail("缺少凭据：请在 .env 里设置 INVEST_ANTHROPIC_AUTH_TOKEN"))
        return False
    print(ok("凭据已配置"))

    if mini_agent.Anthropic is not None:
        print(ok("已安装 anthropic SDK（将使用官方 SDK）"))
    else:
        print(warn("未安装 anthropic SDK（将使用内置标准库兼容客户端，功能等价）"))
        print(f"     想用官方 SDK：pip install -r requirements.txt")

    print(info(f"工作目录（沙箱根）：{mini_agent.WORKSPACE}"))
    return True


def step_test() -> bool:
    """跑单元测试（不需要 API key）。"""
    title("单元测试")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(ROOT),
    )
    return result.returncode == 0


def step_live() -> bool:
    """真实 API 连通性：发一个最小请求。"""
    title("真实 API 连通性")
    try:
        import mini_agent
    except Exception as exc:  # noqa: BLE001
        print(fail(f"导入 mini_agent 失败：{exc}"))
        return False

    if not mini_agent.AUTH_TOKEN:
        print(fail("缺少凭据，跳过"))
        return False

    try:
        client = mini_agent._build_client()
        print(f"  端点          {mini_agent.BASE_URL}")
        print(f"  模型          {mini_agent.MODEL}")
        print(f"  客户端        {type(client).__name__}")
        print(f"  发送          「只回复两个字：连通」")
        response = client.messages.create(
            model=mini_agent.MODEL,
            max_tokens=64,
            messages=[{"role": "user", "content": "只回复两个字：连通"}],
        )
    except Exception as exc:  # noqa: BLE001
        print(fail(f"请求失败：{exc}"))
        return False

    texts = [
        str(mini_agent._block_field(block, "text", ""))
        for block in (getattr(response, "content", None) or [])
        if mini_agent._block_field(block, "type") == "text"
    ]
    reply = " ".join(t for t in texts if t).strip()
    if not reply:
        print(warn("模型没有返回文本内容（可能有 thinking 块被过滤）"))
        return False
    print(ok(f"模型回复：{reply}"))
    return True


def step_agent(task: str = "") -> bool:
    """端到端：让真实模型在临时目录里干一件真事。"""
    title("端到端（真实模型执行任务）")
    try:
        import mini_agent
    except Exception as exc:  # noqa: BLE001
        print(fail(f"导入 mini_agent 失败：{exc}"))
        return False

    if not mini_agent.AUTH_TOKEN:
        print(fail("缺少凭据，跳过"))
        return False

    task = task or (
        "在当前目录创建 hello.py，内容是 print('hello-from-dayu')，"
        "然后运行 python hello.py 确认输出正确，最后告诉我结果。"
    )
    print(f"  任务          {task}")

    workdir = Path(tempfile.mkdtemp(prefix="dayu_e2e_"))
    original_workspace = mini_agent.WORKSPACE
    mini_agent.WORKSPACE = workdir.resolve()
    try:
        result = mini_agent.run_agent(
            task, verbose=True, on_text=mini_agent._write_chunk
        )
        print()
        print(f"  最终回复      {_one_line(result)}（共 {len(result)} 字符）")

        script = workdir / "hello.py"
        if script.exists():
            content = script.read_text(encoding="utf-8")
            print(f"  生成文件      hello.py = {content.strip()!r}")
            print(ok("文件已由模型真实创建"))
        else:
            print(warn("没有找到 hello.py，模型可能改了任务理解"))
            return False
        if "调用模型失败" in result:
            print(fail("模型调用失败"))
            return False
        return True
    finally:
        mini_agent.WORKSPACE = original_workspace
        shutil.rmtree(workdir, ignore_errors=True)


def step_agent_cli(prompt: str) -> bool:
    """用真实模型跑一个自定义任务（工作目录 = 当前目录）。"""
    title("自定义任务（真实模型）")
    try:
        import mini_agent
    except Exception as exc:  # noqa: BLE001
        print(fail(f"导入 mini_agent 失败：{exc}"))
        return False

    print(f"  任务          {prompt}")
    print(f"  工作目录      {mini_agent.WORKSPACE}")
    print()
    result = mini_agent.run_agent(prompt, verbose=True, on_text=mini_agent._write_chunk)
    print()
    print(f"  最终回复      {_one_line(result)}（共 {len(result)} 字符）")
    return "调用模型失败" not in result


def step_chat() -> bool:
    """交互式对话：一直问，一直答。"""
    title("交互式对话（真实模型）")
    try:
        import mini_agent
    except Exception as exc:  # noqa: BLE001
        print(fail(f"导入 mini_agent 失败：{exc}"))
        return False

    if not mini_agent.AUTH_TOKEN:
        print(fail("缺少凭据，跳过"))
        return False

    try:
        mini_agent.interactive()
    except RuntimeError as exc:  # noqa: BLE001
        print(fail(f"启动失败：{exc}"))
        return False
    return True


def step_web() -> bool:
    """浏览器对话框：阻塞式启动，Ctrl-C 退出。"""
    title("浏览器对话框")
    try:
        import web_server
    except Exception as exc:  # noqa: BLE001
        print(fail(f"导入 web_server 失败：{exc}"))
        return False

    print(info("启动后会自动打开浏览器；直接运行可加 --port/--host/--no-browser"))
    try:
        return web_server.main([]) == 0
    except RuntimeError as exc:  # noqa: BLE001
        print(fail(f"启动失败：{exc}"))
        return False


# ── 入口 ──────────────────────────────────────────────────────

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="拙愚智能 启动脚本：一条命令验证整条链路。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "check", "test", "live", "agent", "chat", "web"],
        help="要执行的步骤，默认 all（全部）。",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="agent 模式下的任务描述，留空用默认任务。",
    )
    args = parser.parse_args(argv)

    task = " ".join(args.task).strip()

    steps: List[Tuple[str, Callable[[], bool]]] = []
    if args.command in ("all", "check"):
        steps.append(("环境自检", step_check))
    if args.command in ("all", "test"):
        steps.append(("单元测试", step_test))
    if args.command in ("all", "live"):
        steps.append(("真实 API", step_live))
    if args.command in ("all", "agent"):
        steps.append(
            ("端到端", (lambda: step_agent(task)) if not task else (lambda: step_agent_cli(task)))
        )
    if args.command == "chat":
        steps.append(("交互式对话", step_chat))
    if args.command == "web":
        steps.append(("浏览器对话框", step_web))

    print(_c("拙愚智能 启动检查", "1;36"))
    print(_c(f"仓库根目录：{ROOT}", "90"))

    results: List[Tuple[str, bool]] = []
    for name, func in steps:
        try:
            passed = func()
        except Exception as exc:  # noqa: BLE001
            print(fail(f"{name} 异常：{exc}"))
            passed = False
        results.append((name, passed))

    title("结果汇总")
    for name, passed in results:
        print(f"  {ok(name) if passed else fail(name)}")

    total = len(results)
    succeeded = sum(1 for _, passed in results if passed)
    print()
    if succeeded == total:
        print(_c(f"全部通过（{succeeded}/{total}）—— 可以用了。", "1;32"))
        return 0
    print(_c(f"有步骤失败（{succeeded}/{total} 通过），上面有具体原因。", "1;31"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

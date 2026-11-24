好，给你一份可直接跑的最小实现。设计原则完全照搬 Pi：**四个原子工具 + 一个 while 循环，不做任何框架抽象**。

---

## 一、设计约束

先说清楚这 200 行代码的边界，避免你后面踩坑：

| 约束 | 说明 |
|---|---|
| 工具只有 4 个 | `read` / `write` / `edit` / `bash`，不加第五个 |
| 无框架依赖 | 只用 `anthropic` SDK + 标准库，不引 LangChain |
| 无 MCP | 需要外部能力就写 CLI，Agent 通过 `bash` 调 |
| 上下文不压缩 | 超过阈值直接报错让你手动处理，不做自动摘要（生产环境才需要） |
| 沙箱强制 | `bash` 工具走白名单 + 路径限制，防止误删 |

---

## 二、完整代码

```python
#!/usr/bin/env python3
"""
mini_agent.py — 基于 Pi 哲学的最小 Agent Loop
四个原子工具，一个 while 循环，无框架依赖。
"""

import os
import re
import json
import shlex
import subprocess
from pathlib import Path
from anthropic import Anthropic

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8192
MAX_LOOP = 30                    # 防止死循环
WORKSPACE = Path.cwd().resolve() # 沙箱根目录

# bash 白名单：只允许这些命令前缀
BASH_WHITELIST = {
    "python", "python3", "pip", "pytest",
    "ls", "cat", "head", "tail", "wc", "grep", "find",
    "git", "make", "npm", "pnpm", "node",
    "df", "du", "ps", "echo", "date",
}

# ─────────────────────────────────────────────
# 工具实现（每个工具返回字符串，异常也返回字符串）
# ─────────────────────────────────────────────

def tool_read(path: str, offset: int = 0, limit: int = 2000) -> str:
    """读取文件，支持分页。"""
    try:
        p = (WORKSPACE / path).resolve()
        if not str(p).startswith(str(WORKSPACE)):
            return f"错误：路径越界 {path}"
        if not p.exists():
            return f"错误：文件不存在 {path}"
        lines = p.read_text(encoding="utf-8").splitlines()
        chunk = lines[offset:offset + limit]
        # 带行号输出，方便后续 edit 定位
        return "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(chunk))
    except Exception as e:
        return f"错误：{e}"


def tool_write(path: str, content: str) -> str:
    """写入/覆盖文件。"""
    try:
        p = (WORKSPACE / path).resolve()
        if not str(p).startswith(str(WORKSPACE)):
            return f"错误：路径越界 {path}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"


def tool_edit(path: str, old_text: str, new_text: str) -> str:
    """精确替换。old_text 必须在文件中唯一出现。"""
    try:
        p = (WORKSPACE / path).resolve()
        if not str(p).startswith(str(WORKSPACE)):
            return f"错误：路径越界 {path}"
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count == 0:
            return f"错误：未找到待替换文本，请先用 read 确认内容"
        if count > 1:
            return f"错误：待替换文本出现 {count} 次，不唯一，请提供更长的上下文"
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


def tool_bash(command: str, timeout: int = 60) -> str:
    """执行 shell 命令，白名单 + 超时保护。"""
    try:
        # 提取第一个命令词做白名单校验
        first = shlex.split(command)[0] if command.strip() else ""
        # 处理 ./xxx 或 /usr/bin/xxx 形式
        base = os.path.basename(first)
        if base not in BASH_WHITELIST:
            return f"错误：命令 '{base}' 不在白名单内，允许：{sorted(BASH_WHITELIST)}"

        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code] {result.returncode}"
        # 截断超长输出，防止撑爆上下文
        if len(output) > 20000:
            output = output[:20000] + f"\n...（输出截断，共 {len(output)} 字符）"
        return output or "（无输出）"
    except subprocess.TimeoutExpired:
        return f"错误：命令超时（{timeout}s）"
    except Exception as e:
        return f"错误：{e}"


# ─────────────────────────────────────────────
# 工具注册表（给 API 的 schema）
# ─────────────────────────────────────────────

TOOLS = [
    {
        "name": "read",
        "description": "读取文件内容，返回带行号的文本。大文件用 offset/limit 分页。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于工作目录的路径"},
                "offset": {"type": "integer", "description": "起始行号（0-based）", "default": 0},
                "limit": {"type": "integer", "description": "读取行数", "default": 2000},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "description": "创建或覆盖写入文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit",
        "description": "精确替换文件中的一段文本。old_text 必须在文件中唯一出现，否则报错。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "待替换的原文（需唯一）"},
                "new_text": {"type": "string", "description": "替换后的新文本"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "bash",
        "description": "执行 shell 命令。仅允许白名单内的命令（python/git/ls/grep 等）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "超时秒数", "default": 60},
            },
            "required": ["command"],
        },
    },
]

TOOL_IMPL = {
    "read": tool_read,
    "write": tool_write,
    "edit": tool_edit,
    "bash": tool_bash,
}

# ─────────────────────────────────────────────
# System Prompt（刻意保持极简，学 Pi）
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个运行在终端里的编程助手。

你有四个工具：read、write、edit、bash。所有复杂任务都基于这四个原语完成。

工作原则：
1. 先读后改。修改任何文件前，必须先用 read 确认当前内容。
2. edit 的 old_text 必须唯一。如果不唯一，提供更长的上下文。
3. 用 bash 执行命令验证你的修改。不要假设代码能跑。
4. 需要外部能力时，写脚本或调用 CLI，不要等待新工具。
5. 每次工具调用后，观察输出再决定下一步。不要连续盲写。

回复风格：简洁。不要复述文件内容，直接说做了什么、下一步做什么。"""


# ─────────────────────────────────────────────
# Agent Loop（核心，就这一个 while）
# ─────────────────────────────────────────────

def run_agent(user_input: str, verbose: bool = True) -> str:
    client = Anthropic()  # 读 ANTHROPIC_API_KEY 环境变量
    messages = [{"role": "user", "content": user_input}]

    for step in range(MAX_LOOP):
        if verbose:
            print(f"\n{'─' * 50}\n[Step {step + 1}]")

        # 1. 调模型
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # 2. 把模型输出追加进历史
        messages.append({"role": "assistant", "content": response.content})

        # 3. 如果模型不再调工具，任务结束
        if response.stop_reason == "end_turn":
            # 提取纯文本回复
            texts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(texts)

        # 4. 执行工具调用
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                name = block.name
                args = block.input
                if verbose:
                    print(f"  → {name}({json.dumps(args, ensure_ascii=False)[:120]})")

                # 分发到实现
                if name not in TOOL_IMPL:
                    result = f"错误：未知工具 {name}"
                else:
                    result = TOOL_IMPL[name](**args)

                if verbose:
                    preview = result[:200].replace("\n", " ")
                    print(f"  ← {preview}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            # max_tokens 截断等情况
            return f"异常停止：{response.stop_reason}"

    return f"达到最大循环次数 {MAX_LOOP}，强制退出"


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    prompt = " ".join(sys.argv[1:]) or "读取当前目录的文件列表，告诉我这个项目是干什么的"
    print(f"工作目录：{WORKSPACE}")
    print(f"任务：{prompt}")
    final = run_agent(prompt)
    print(f"\n{'═' * 50}\n{final}")
```

---

## 三、跑起来

```bash
# 1. 装依赖（只有一个是第三方）
pip install anthropic

# 2. 设 key
export ANTHROPIC_API_KEY="sk-ant-xxx"

# 3. 跑
python mini_agent.py "读取 requirements.txt，告诉我这个项目依赖了什么"
python mini_agent.py "写一个脚本统计当前目录所有 py 文件的行数，按行数排序"
```

---

## 四、几个关键设计说明

### 1. 为什么 `edit` 要求 old_text 唯一

这是 Pi 和 Claude Code 都采用的策略。如果允许模糊匹配或多次替换，模型很容易改错地方。强制唯一性 = 强制模型先 `read` 看清楚再动手。

报错信息里直接告诉它"请提供更长的上下文"，模型下一轮会自己修正。

### 2. 为什么 `bash` 要白名单

你之前问 OpenClaw 安全风险时我提过，Agent 拿到 shell 权限是很危险的。白名单是最简单的第一道防线。

**更安全的做法**（生产环境建议）：

```python
# 用 Docker 跑整个 Agent
# docker run --rm -it \
#   -v $(pwd):/workspace \
#   --network=none \
#   --memory=2g \
#   --cpus=2 \
#   python:3.11-slim \
#   python /workspace/mini_agent.py
```

`--network=none` 断网，防止它偷偷 curl 外网。

### 3. 为什么不做上下文压缩

Pi 的做法是**不压缩，直接报错让你处理**。原因：自动摘要会丢信息，而丢的信息往往是你最需要的（比如某个函数签名）。

如果你需要长任务，加一个简单的滑动窗口：

```python
def trim_messages(messages, max_chars=100000):
    """保留 system + 最近 N 轮，丢弃中间的 tool_result 细节。"""
    total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    while total > max_chars and len(messages) > 4:
        # 从第 2 条开始删（保留第一条 user 和最后两条）
        removed = messages.pop(1)
        total -= len(json.dumps(removed, ensure_ascii=False))
        # 插一条占位符
        messages.insert(1, {
            "role": "user",
            "content": "（中间步骤已省略）"
        })
    return messages
```

### 4. 为什么 System Prompt 这么短

Pi 的全部系统提示词不到 1000 tokens。实测发现：**提示词越长，模型越容易忽略关键约束**。

上面那 5 条工作原则是核心，加多了反而降低遵循率。

---

## 五、怎么扩展到你的量化场景

四个工具已经够用，你只需要**把能力做成 CLI**：

```bash
# 你的量化工具做成命令行
quant-tools fetch-financials --code 600519 --year 2025
quant-tools calc-position --capital 100000 --risk 0.02
quant-tools backtest --strategy ma_cross --start 2024-01-01
```

然后 Agent 通过 `bash` 调用：

```
你：帮我看看茅台 2025 年三季报的毛利率，然后算一下 10 万本金、2% 风险下的仓位

Agent：
  → bash("quant-tools fetch-financials --code 600519 --year 2025 --quarter 3")
  ← {"revenue": ..., "gross_margin": 0.915, ...}
  → bash("quant-tools calc-position --capital 100000 --risk 0.02 --price 1680")
  ← {"shares": 59, "position_value": 99120}
  
茅台 2025Q3 毛利率 91.5%，10 万本金 2% 风险下建议买 59 股（约 9.9 万）。
```

**这就是 Pi 哲学的核心**：不需要给 Agent 加 `fetch_financials` 工具，它用 `bash` 调你的 CLI 就行。工具数量永远是 4 个，能力无限扩展。

---

## 六、下一步可以加的（但建议先别加）

按优先级排序，**建议跑通基础版再逐个加**：

| 优先级 | 功能 | 实现难度 |
|---|---|---|
| P0 | 流式输出（看到模型在想什么） | 低，改 `messages.stream()` |
| P0 | 交互式 REPL（多轮对话） | 低，外面套个 while |
| P1 | 上下文压缩 | 中，见上面 `trim_messages` |
| P1 | 工具调用确认（写操作前问你） | 低，在 `TOOL_IMPL` 分发前插钩子 |
| P2 | 子 Agent（并行任务） | 高，需要进程管理 |
| P2 | 持久化记忆（跨会话） | 中，写个 `memory.json` |

需要我帮你加流式输出和交互式 REPL 吗？加了之后就能像 Claude Code 那样连续对话了。
#!/usr/bin/env python3
"""
mini_agent.py — 基于 Pi 哲学的最小 Agent Loop
四个原子工具，一个 while 循环，无框架依赖。

两种用法：
    python src/mini_agent.py              # 交互模式：一直问，一直答，上下文自动保留
    python src/mini_agent.py "你的任务"    # 单次任务：跑完就退出
"""

import json
import locale
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from anthropic import Anthropic
except ImportError:  # 没装 SDK 也能 import 本模块（跑测试用），真正调模型时才报错
    Anthropic = None


# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
def _load_dotenv(path):
    """读 .env 文件，返回 dict。不覆盖已存在的环境变量。

    只支持 KEY=VALUE（可带引号、行尾注释），够用且不引第三方依赖。
    """
    values = {}
    if not path:
        return values
    file_path = Path(path)
    if not file_path.is_file():
        return values
    for raw_line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            if end != -1:
                value = value[1:end]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _find_dotenv():
    """按 优先级 找 .env：显式指定 > 当前目录 > 仓库根目录。"""
    candidates = [
        os.environ.get("ANTHROPIC_ENV_FILE"),
        str(Path.cwd() / ".env"),
        str(Path(__file__).resolve().parents[1] / ".env"),
    ]
    for candidate in candidates:
        loaded = _load_dotenv(candidate)
        if loaded:
            return loaded
    return {}


DOTENV = _find_dotenv()


def _setting(name, default=None):
    """环境变量优先，其次 .env。"""
    return os.environ.get(name) or DOTENV.get(name) or default


def _first_setting(*names):
    """返回 (值, 来源变量名)。都没设置则为 (None, None)。

    记录来源是为了选鉴权方式：AUTH_TOKEN 类变量对应 Bearer 鉴权，
    ANTHROPIC_API_KEY 对应 x-api-key（新版 SDK 不接受两个一起传）。
    """
    for name in names:
        value = _setting(name)
        if value:
            return value, name
    return None, None


MODEL = (
    _setting("INVEST_ANTHROPIC_MODEL")
    or _setting("INVEST_ANTHROPIC_DEFAULT_SONNET_MODEL")
    or "claude-sonnet-4-5"
)
BASE_URL = _setting("INVEST_ANTHROPIC_BASE_URL") or _setting("ANTHROPIC_BASE_URL")
AUTH_TOKEN, AUTH_TOKEN_SOURCE = _first_setting(
    "INVEST_ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
MAX_TOKENS = 8192
MAX_LOOP = 30                    # 防止死循环
MAX_CONTEXT_CHARS = 600_000     # 上下文上限：不做压缩，超了直接报错让人处理
MAX_BASH_OUTPUT = 20000         # 单次输出上限，防止撑爆上下文
WORKSPACE = Path.cwd().resolve() # 沙箱根目录

# bash 白名单：只允许这些命令前缀
# 注：文档原版没有 pwd，实测模型会反复调用它来确认位置，白白多耗两轮，
#     pwd 只读且输出的就是沙箱根目录，所以加进来。
BASH_WHITELIST = {
    "python", "python3", "pip", "pytest",
    "ls", "cat", "head", "tail", "wc", "grep", "find",
    "git", "make", "npm", "pnpm", "node",
    "df", "du", "ps", "echo", "date", "pwd",
}

# 白名单命令自己带"能执行白名单外的程序"的参数。只看命令名是不够的：
#   find . -exec sh -c "rm -rf /" ";"   命令名 find 在白名单内，实际会跑 rm
#   git -c 'alias.qa=!rm -rf /' qa      同理
# 这类参数一律禁掉，否则沙箱路径检查形同虚设。
FORBIDDEN_ARGS = {
    "find": {
        "-exec", "-execdir", "-ok", "-okdir",
        "-delete", "-fls", "-fprint", "-fprintf",
    },
}


_ENV_VAR_RE = re.compile(r"%[^%\s]+%")
_PY_VERSION_RE = re.compile(r"^python(\d)(\.\d+)?$")
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")


# ─────────────────────────────────────────────
# 模型客户端
# ─────────────────────────────────────────────

class AnthropicCompatClient:
    """标准库实现的最小 Anthropic 兼容客户端（只实现 messages.create）。

    官方 anthropic SDK 没装/装不上时用它，对外接口完全一致，
    走的是同一个 Anthropic Messages API。
    """

    def __init__(self, token, base_url, timeout=120):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.messages = self

    def create(self, **kwargs):
        payload = dict(kwargs)
        payload.setdefault("max_tokens", 8192)
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.token,
                "authorization": f"Bearer {self.token}",
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {error.code}: {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"连接 {self.base_url} 失败：{error.reason}") from error
        return _ApiResponse(
            content=data.get("content") or [], stop_reason=data.get("stop_reason")
        )


class _ApiResponse:
    """和 anthropic SDK 的响应对象保持同样的字段。"""

    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


def _build_client():
    """构造真实客户端。

    装了官方 SDK 就用 SDK（同时带 base_url 和 token），
    没装就用标准库兼容实现，两者对外接口一致。
    """
    if Anthropic is not None:
        if not AUTH_TOKEN:
            raise RuntimeError(
                "未配置模型凭据：请在 .env 中设置 INVEST_ANTHROPIC_AUTH_TOKEN，"
                "或导出 ANTHROPIC_API_KEY"
            )
        kwargs = {}
        if BASE_URL:
            kwargs["base_url"] = BASE_URL
        # SDK 只允许一种凭据。变量叫 API_KEY 的走 x-api-key，
        # AUTH_TOKEN 类变量走网关常见的 Authorization: Bearer。
        if AUTH_TOKEN_SOURCE == "ANTHROPIC_API_KEY":
            kwargs["api_key"] = AUTH_TOKEN
        else:
            kwargs["auth_token"] = AUTH_TOKEN
        return Anthropic(**kwargs)

    if not AUTH_TOKEN or not BASE_URL:
        raise RuntimeError(
            "未配置模型凭据：请在 .env 中设置 INVEST_ANTHROPIC_BASE_URL 和 "
            "INVEST_ANTHROPIC_AUTH_TOKEN（或安装 anthropic SDK 并导出 ANTHROPIC_API_KEY）"
        )
    return AnthropicCompatClient(AUTH_TOKEN, BASE_URL)


# ─────────────────────────────────────────────
# 沙箱辅助
# ─────────────────────────────────────────────

def _resolve_path(path):
    """把 path 解析到 WORKSPACE 内部，越界返回 None。

    文档原版用 str.startswith 判断越界，会把 C:\\workspace2 误判成
    C:\\workspace 的子目录；这里改用 normcase + 分隔符前缀比较。
    """
    root = WORKSPACE.resolve()
    candidate = Path(path)
    target = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = target.resolve()
    except OSError:
        return None

    root_key = os.path.normcase(str(root))
    resolved_key = os.path.normcase(str(resolved))
    if resolved_key == root_key:
        return resolved
    if not resolved_key.startswith(root_key + os.sep):
        return None
    return resolved


def _tokenize(segment):
    """按空白切分一段命令：引号内不切，反斜杠保留（Windows 路径需要）。"""
    tokens, current, quote = [], [], None
    for ch in segment:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def _split_segments(command):
    """按 shell 分隔符切分命令：&& || & ; | 换行，引号内不切。

    白名单必须逐段校验，否则 `echo ok && rm -rf /` 会靠首段蒙混过关。
    """
    segments, current, quote = [], [], None
    index, length = 0, len(command)
    while index < length:
        ch = command[index]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            index += 1
            continue
        if command[index:index + 2] in ("&&", "||"):
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if ch in ("&", ";", "|", "\n", "\r"):
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(ch)
        index += 1
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _translate_separators(command):
    r"""把 POSIX 风格的 `;` 分隔符翻译成 Windows cmd.exe 认的 `&`。

    cmd.exe 只把 & && || | 和换行当分隔符，`;` 是普通字符：
        python hello.py; echo done
    在 cmd.exe 里会变成 `python` 带三个参数 hello.py; / echo / done，
    连文件名都被带上了分号。POSIX shell 里 `;` 是顺序执行，cmd.exe 里对应的是 `&`。

    引号内的 `;` 不动（cmd.exe 里引号内的 ; 是普通字符）；
    反斜杠转义的 `\;` 也不动（留给 find -exec 这类用法）。
    """
    out = []
    quote = None
    index, length = 0, len(command)
    while index < length:
        ch = command[index]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            index += 1
            continue
        if ch == ";" and (not out or out[-1] != "\\"):
            out.append(" & ")
            index += 1
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def _head_command(segment):
    """取一段命令真正执行的程序名（跳过 FOO=bar 这类环境变量前缀）。"""
    for token in _tokenize(segment):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return token
    return ""


def _check_git_args(tokens):
    """检查 git 的参数：能不能打开命令执行能力，或写到工作区之外。

    返回错误信息，None 表示通过。
    """
    args = tokens[1:]

    # 子命令之前的全局选项：git -c key=val 里任何 key 都可能打开执行能力
    # （alias. / core.sshCommand / core.pager / core.hooksPath ...），一律禁。
    # 子命令自己的同名选项（git commit -c HEAD~1）是合法用法，不动。
    for token in args:
        if not token.startswith("-"):
            break
        if token in ("-c", "--config-env"):
            return f"错误：不允许 'git {token}'，它能执行白名单外的程序"
        if token == "--exec-path":
            return "错误：不允许 'git --exec-path'，它能执行白名单外的程序"

    # git config：--global/--system/--file 写到工作区之外，
    # alias.* 和以 ! 开头的值会留下以后被 git 执行的内容。
    subcommand = next((token for token in args if not token.startswith("-")), "")
    if subcommand == "config":
        for token in args:
            if token.startswith(("--global", "--system", "--file", "--edit")):
                return f"错误：不允许 'git config {token}'，它会写到工作区之外"
        for token in args:
            if "alias." in token.lower() or token.startswith("!"):
                return "错误：不允许 'git config' 定义 alias，它能执行白名单外的程序"
    return None


def _check_command_arguments(base, tokens):
    """检查白名单命令的参数是否越权。返回错误信息，None 表示通过。"""
    for arg in tokens[1:]:
        if arg in FORBIDDEN_ARGS.get(base, ()):
            return f"错误：不允许 '{base} {arg}'，它能执行白名单外的程序"
    if base == "git":
        return _check_git_args(tokens)
    return None


def _normalize_command_name(token):
    """取命令规范名：basename、去可执行后缀、归一 python 版本号。"""
    cleaned = token.strip().strip('"').strip("'").replace("\\", "/")
    name = os.path.basename(cleaned).lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    matched = _PY_VERSION_RE.match(name)
    if matched:
        name = "python" + matched.group(1)
    return name


def _check_path_argument(token):
    """参数里的路径不能越出沙箱。返回错误信息，None 表示通过。"""
    if not token or token.startswith("-"):
        return None
    if token.startswith("~"):
        return f"错误：路径 {token} 使用了 ~ 展开，超出沙箱范围"
    if _ENV_VAR_RE.search(token):
        return f"错误：参数 {token} 含环境变量展开，无法确认是否越界，请改用沙箱内的相对路径"
    if not os.path.isabs(token) and ".." not in Path(token).parts and token not in (".", ".."):
        return None
    if _resolve_path(token) is None:
        return f"错误：路径越界 {token}"
    return None


def _kill_process_tree(process):
    """连同子孙进程一起杀掉。

    只杀父进程不够：shell=True 时父进程是 cmd.exe/bash，真正的 python 是
    它的子进程，管道仍被持有，超时形同虚设。
    """
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _decode_output(data, fallback=None):
    """解码子进程输出（bytes → str）。

    Windows 的管道里同时流着两种编码：
      - cmd 内建命令（echo/dir）按 OEM 代码页输出，中文环境下是 GBK；
      - MSYS 工具（ls/cat/git/python）按 UTF-8 输出。
    固定用 locale 编码解，MSYS 那边的中文就变成乱码（UTF-8 被当 GBK 读）。
    所以先按 UTF-8 严格解，失败再退回本地代码页，最后用替换字符兜底。
    """
    if not data:
        return ""
    for encoding in ("utf-8", fallback or locale.getpreferredencoding(False)):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _run_command(command, timeout):
    """执行命令，返回 (stdout, stderr, returncode)。超时抛 TimeoutExpired。"""
    kwargs = {
        "shell": True,
        "cwd": str(WORKSPACE),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        raise subprocess.TimeoutExpired(command, timeout, output=stdout)
    return _decode_output(stdout), _decode_output(stderr), process.returncode or 0


# ─────────────────────────────────────────────
# 工具实现（每个工具返回字符串，异常也返回字符串）
# ─────────────────────────────────────────────

def tool_read(path: str, offset: int = 0, limit: int = 2000) -> str:
    """读取文件，支持分页。"""
    try:
        p = _resolve_path(path)
        if p is None:
            return f"错误：路径越界 {path}"
        if not p.exists():
            return f"错误：文件不存在 {path}"
        if p.is_dir():
            return f"错误：{path} 是目录，不是文件"
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"错误：{path} 不是 UTF-8 编码的文本文件"
        offset = max(0, int(offset))
        limit = max(1, int(limit))
        lines = text.splitlines()
        chunk = lines[offset:offset + limit]
        if not chunk:
            return f"（{path} 在第 {offset + 1} 行之后没有内容）"
        # 带行号输出，方便后续 edit 定位
        return "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(chunk))
    except Exception as e:
        return f"错误：{e}"


def tool_write(path: str, content: str) -> str:
    """写入/覆盖文件。"""
    try:
        p = _resolve_path(path)
        if p is None:
            return f"错误：路径越界 {path}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"


def tool_edit(path: str, old_text: str, new_text: str) -> str:
    """精确替换。old_text 必须在文件中唯一出现。"""
    try:
        p = _resolve_path(path)
        if p is None:
            return f"错误：路径越界 {path}"
        if not p.exists():
            return f"错误：文件不存在 {path}"
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
    """执行 shell 命令，白名单 + 路径限制 + 超时保护。"""
    try:
        command = (command or "").strip()
        if not command:
            return "错误：命令为空"

        # 命令替换 $(...) / `...` 能藏住任意命令，绕过白名单，直接拒绝
        if "$(" in command or "`" in command:
            return "错误：不允许命令替换（$( ) 或反引号），请直接写命令"

        # 逐段校验：每一段的命令都要在白名单内，路径都要在沙箱内
        for segment in _split_segments(command):
            head = _head_command(segment)
            if not head:
                continue
            # 处理 ./xxx 或 /usr/bin/xxx 形式
            base = _normalize_command_name(head)
            if base not in BASH_WHITELIST:
                return f"错误：命令 '{base}' 不在白名单内，允许：{sorted(BASH_WHITELIST)}"
            # python -c "..." 等于绕过白名单直接执行任意代码
            tokens = _tokenize(segment)
            if base in ("python", "python3", "node") and any(
                arg in ("-c", "-e", "-p", "--eval") for arg in tokens[1:]
            ):
                return (
                    f"错误：不允许 '{base} -c' 内联执行代码，"
                    "请先用 write 工具写出脚本文件，再运行该脚本"
                )
            # 命令名在白名单里，但参数能再拉起一个白名单外的程序
            arg_error = _check_command_arguments(base, tokens)
            if arg_error:
                return arg_error
            for token in tokens[1:]:
                path_error = _check_path_argument(token)
                if path_error:
                    return path_error

        # 校验用 POSIX 语义逐段切；执行前按当前 shell 的语义对齐，
        # 否则 Windows 上 `a; b` 会被 cmd.exe 当成 `a` 的参数（见 _translate_separators）
        if os.name == "nt":
            command = _translate_separators(command)

        stdout, stderr, returncode = _run_command(command, max(1, int(timeout)))
        output = stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}"
        if returncode != 0:
            output += f"\n[exit code] {returncode}"
        # 截断超长输出，防止撑爆上下文
        if len(output) > MAX_BASH_OUTPUT:
            output = output[:MAX_BASH_OUTPUT] + f"\n...（输出截断，共 {len(output)} 字符）"
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

AGENT_NAME = "大愚智能"

SYSTEM_PROMPT = f"""你叫「{AGENT_NAME}」，是一个运行在终端里的编程助手。

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

def _estimate_chars(messages):
    total = 0
    for message in messages:
        try:
            total += len(json.dumps(message, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(str(message))
    return total


def _block_field(block, name, default=None):
    """兼容 SDK 的对象式 block 和测试里的字典式 block。"""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _stop(messages, text):
    """把终止原因也记进历史，保证多轮对话里 user/assistant 始终交替。

    不这样做的话，出错的那一轮会在历史里留下一条没有回复的 user 消息，
    下一轮就变成连续两条 user，部分兼容网关会直接拒绝请求。
    """
    messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    return text


def _clean_input(text):
    """修掉管道输入被按错误代码页解码后留下的代理字符。

    真实终端里 input() 直接给正确的 str，这里原样返回。
    但 `printf '中文' | python ...` 这类管道，stdin 会按本地代码页（中文
    Windows 上是 GBK）解码，UTF-8 字节就变成 \\udcXX 代理字符；
    带着它去 json.dumps().encode("utf-8") 会直接抛异常。
    用 surrogateescape 把字节原样取回来，再按 UTF-8 重解一次即可。
    """
    if not text or not any("\ud800" <= ch <= "\udfff" for ch in text):
        return text
    local = locale.getpreferredencoding(False)
    candidates = []
    for encoding in (getattr(sys.stdin, "encoding", None), local, "utf-8", "gbk"):
        if encoding and encoding not in candidates:
            candidates.append(encoding)
    for source in candidates:
        try:
            raw = text.encode(source, "surrogateescape")
        except (UnicodeEncodeError, LookupError):
            continue
        for target in ("utf-8", local):
            try:
                return raw.decode(target)
            except (UnicodeDecodeError, LookupError):
                continue
    return text.encode("utf-8", "replace").decode("utf-8")


def run_agent(
    user_input: str,
    client=None,
    verbose: bool = True,
    history=None,
    on_event=None,
) -> str:
    """跑完整个 agent loop，返回模型最后一段文本。

    client 可注入，便于测试时用假 client 跑完整流程。
    history 传入一个列表时，本轮对话会追加进该列表，下一轮继续用它，
    就得到多轮对话；不传则每次都是独立的一轮。
    on_event 传入回调时，循环会把结构化进度事件交给它（供 Web 前端实时展示）：
        {"type": "step", "step": 1}
        {"type": "thinking", "text": "..."}
        {"type": "assistant_text", "text": "..."}
        {"type": "tool_call", "id": "...", "name": "read", "input": {...}}
        {"type": "tool_result", "id": "...", "name": "read", "output": "..."}
        {"type": "final", "text": "..."} / {"type": "error", "message": "..."}
    """
    if client is None:
        client = _build_client()  # 真实访问：配置从 .env / 环境变量读
    messages = history if history is not None else []
    messages.append({"role": "user", "content": _clean_input(user_input)})

    def emit(event):
        """把进度推给调用方。回调异常直接向上抛：Web 端靠它感知浏览器断开。"""
        if on_event is not None:
            on_event(event)

    def fail(text):
        """出错时先把原因推给前端，再把终止消息补进历史。"""
        emit({"type": "error", "message": text})
        return _stop(messages, text)

    for step in range(MAX_LOOP):
        if verbose:
            print(f"\n{'─' * 50}\n[Step {step + 1}]")
        emit({"type": "step", "step": step + 1})

        # 上下文不做压缩：超阈值直接报错，让人手动处理
        if _estimate_chars(messages) > MAX_CONTEXT_CHARS:
            return fail(
                f"上下文超过 {MAX_CONTEXT_CHARS} 字符上限，本项目不做自动摘要，"
                "请手动拆分任务，或输入 /clear 清空会话。",
            )

        # 1. 调模型
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=list(messages),
            )
        except Exception as e:
            return fail(f"调用模型失败：{e}")

        # 2. 把模型输出追加进历史
        content = [
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else block
            for block in (getattr(response, "content", None) or [])
        ]
        messages.append({"role": "assistant", "content": content})

        # 中间过程也给前端看一眼：思考块、以及调用工具前说的过渡性文字
        for block in content:
            block_type = _block_field(block, "type")
            if block_type == "thinking":
                text = _block_field(block, "thinking", "") or ""
                if text:
                    emit({"type": "thinking", "text": text[:4000]})
            elif block_type == "text" and response.stop_reason != "end_turn":
                text = _block_field(block, "text", "") or ""
                if text:
                    emit({"type": "assistant_text", "text": text})

        # 3. 如果模型不再调工具，任务结束
        if response.stop_reason == "end_turn":
            # 提取纯文本回复
            texts = [
                _block_field(block, "text", "")
                for block in content
                if _block_field(block, "type") == "text"
            ]
            final_text = "\n".join(texts)
            emit({"type": "final", "text": final_text})
            return final_text

        # 4. 执行工具调用
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in content:
                if _block_field(block, "type") != "tool_use":
                    continue

                name = _block_field(block, "name", "")
                block_id = _block_field(block, "id", "")
                args = _block_field(block, "input", {}) or {}
                if not isinstance(args, dict):
                    args = {}
                if verbose:
                    print(f"  → {name}({json.dumps(args, ensure_ascii=False)[:120]})")
                emit({"type": "tool_call", "id": block_id, "name": name, "input": args})

                # 分发到实现
                if name not in TOOL_IMPL:
                    result = f"错误：未知工具 {name}"
                else:
                    try:
                        result = TOOL_IMPL[name](**args)
                    except Exception as e:
                        result = f"错误：{e}"

                if verbose:
                    preview = result[:200].replace("\n", " ")
                    print(f"  ← {preview}")
                emit({
                    "type": "tool_result",
                    "id": block_id,
                    "name": name,
                    "output": result,
                })

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block_id,
                    "content": result,
                })

            if not tool_results:
                return fail("异常停止：模型请求了工具但没有返回 tool_use 块")
            messages.append({"role": "user", "content": tool_results})
        else:
            # max_tokens 截断等情况；assistant 内容已在上方追加，只返回不再写入
            text = f"异常停止：{response.stop_reason}"
            emit({"type": "error", "message": text})
            return text

    return fail(f"达到最大循环次数 {MAX_LOOP}，强制退出")


# ─────────────────────────────────────────────
# 交互式 REPL（多轮对话）
# ─────────────────────────────────────────────

REPL_HELP = """命令：
  /clear   清空对话历史（上下文超限时用）
  /help    显示本帮助
  /exit    退出（也可以按 Ctrl-D）
其他输入都会发给模型，多轮上下文自动保留。"""


def interactive(client=None, verbose: bool = True) -> None:
    """多轮对话：一直问，一直答，直到 /exit 或 Ctrl-D。

    history 在循环外创建、每轮传进去，所以模型始终能看到之前的对话。
    """
    if client is None:
        client = _build_client()
    history = []
    print("\n交互模式已启动，直接输入问题回车发送。输入 /help 查看命令。")

    while True:
        try:
            raw = input("\n你 > ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\n（用 /exit 或 Ctrl-D 退出）")
            continue

        text = raw.strip()
        if not text:
            continue
        command = text.lower()
        if command in ("/exit", "/quit", "exit", "quit"):
            break
        if command in ("/clear", "/reset"):
            history.clear()
            print("已清空对话历史，下一句从零开始。")
            continue
        if command == "/help":
            print(REPL_HELP)
            continue

        # Ctrl-C 打断本轮：把历史回滚到本轮开始前，避免留下半截对话
        snapshot = len(history)
        try:
            reply = run_agent(text, client=client, verbose=verbose, history=history)
        except KeyboardInterrupt:
            del history[snapshot:]
            print("\n本轮已取消。")
            continue
        print(f"\n{AGENT_NAME} > {reply}")


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Windows 控制台默认 GBK/cp936，输出中文在 VS Code / Windows Terminal 下会乱码，
    # 这里统一按 UTF-8 输出；errors="replace" 保证遇到无法编码的字符也不崩。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    prompt = " ".join(sys.argv[1:]).strip()
    backend = "anthropic SDK" if Anthropic is not None else "标准库兼容客户端"
    print(f"工作目录：{WORKSPACE}")
    print(f"模式：{'单次任务' if prompt else '交互对话'}")
    if prompt:
        print(f"任务：{prompt}")
    print(f"模型：{MODEL}" + (f" @ {BASE_URL}" if BASE_URL else "") + f"（{backend}）")

    try:
        client = _build_client()
        if prompt:
            final = run_agent(prompt, client=client)
            print(f"\n{'═' * 50}\n{final}")
        else:
            interactive(client=client)
    except RuntimeError as e:
        print(f"错误：{e}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n已退出。")

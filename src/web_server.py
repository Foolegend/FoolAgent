#!/usr/bin/env python3
"""web_server.py — 给 拙愚智能 套一个浏览器对话框。

只用标准库，不引入任何 Web 框架：

    python src/web_server.py                    # http://127.0.0.1:8765
    python src/web_server.py --port 9000        # 换端口
    python src/web_server.py --no-browser       # 不自动打开浏览器

接口：
    GET  /               前端页面（web/index.html）
    GET  /api/info       模型、工作目录等配置信息
    POST /api/chat       提问；用 SSE 逐步推送 agent 的每个处理步骤
    POST /api/confirm    回答"要不要执行这次操作"的询问
    POST /api/reset      清空某个会话的上下文

安全提示：这个服务把拥有文件读写 / 命令执行能力的 agent 暴露成了 HTTP 接口，
默认只绑定 127.0.0.1。不要在没有额外鉴权和隔离的情况下改成 0.0.0.0。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# 直接运行（python src/web_server.py）和从 run.py 导入（src 已加入 sys.path）都能工作
try:
    import mini_agent
except ImportError:  # pragma: no cover - 直接运行时走这里
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mini_agent


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"

MAX_BODY = 1_000_000        # 单次请求体上限，防止被塞爆内存
MAX_SESSIONS = 200          # 内存里最多保留多少个会话
CONFIRM_TIMEOUT = 180       # 等用户点确认的最长秒数
CONFIRM_PING = 15           # 等待期间每多少秒探一次连接还在不在
SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "close",
}

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/yugong.svg": ("yugong.svg", "image/svg+xml"),
    "/yugong-light.svg": ("yugong-light.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}


# ─────────────────────────────────────────────
# 会话存储
# ─────────────────────────────────────────────

class Session:
    """一个浏览器会话：对话历史 + 串行锁。

    同一会话的并发请求会排队执行，避免两轮对话同时改同一个 history
    导致 user/assistant 顺序错乱；不同会话之间互不影响。
    """

    def __init__(self) -> None:
        self.history: list = []
        self.lock = threading.Lock()


_sessions: "OrderedDict[str, Session]" = OrderedDict()
_sessions_lock = threading.Lock()


def get_session(session_id: str) -> Session:
    """取出会话；没有就建一个。顺带做 LRU 淘汰。"""
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
        if session is None:
            session = Session()
        _sessions[session_id] = session
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
        return session


def reset_session(session_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(session_id, None)


class ClientDisconnected(Exception):
    """浏览器中途断开（关页面 / 点停止 / 网络断）。"""


# ── 操作确认 ────────────────────────────────────────────────
# agent 要写文件 / 改文件 / 执行命令时，先在这里挂一个请求，
# 把询问推给浏览器，等用户点"允许"或"拒绝"再继续。

_pending_confirms: "OrderedDict[str, dict]" = OrderedDict()
_confirm_lock = threading.Lock()


def _register_confirm(request_id: str) -> dict:
    entry = {"event": threading.Event(), "approved": False}
    with _confirm_lock:
        _pending_confirms[request_id] = entry
    return entry


def resolve_confirm(request_id: str, approved: bool) -> bool:
    """回答一次询问。找不到对应请求（已超时 / 已取消）返回 False。"""
    with _confirm_lock:
        entry = _pending_confirms.get(request_id)
    if entry is None:
        return False
    entry["approved"] = bool(approved)
    entry["event"].set()
    return True


def _drop_confirm(request_id: str) -> None:
    with _confirm_lock:
        _pending_confirms.pop(request_id, None)


def make_confirm_callback(emit, timeout: int = CONFIRM_TIMEOUT):
    """做一个确认回调：把询问推给前端，等用户点允许 / 拒绝。

    返回 False 表示拒绝（或超时），agent 不会执行这次操作。
    """

    def confirm(name, args) -> bool:
        request_id = uuid.uuid4().hex
        entry = _register_confirm(request_id)
        try:
            emit({
                "type": "confirm",
                "id": request_id,
                "name": name,
                "input": args,
            })
            deadline = time.monotonic() + timeout
            while True:
                # 分段等：期间探一下连接还在不在，浏览器关了要能立刻发现
                if entry["event"].wait(CONFIRM_PING):
                    break
                if time.monotonic() >= deadline:
                    emit({
                        "type": "confirm_result",
                        "id": request_id,
                        "approved": False,
                        "reason": "等待确认超时",
                    })
                    return False
                emit({"type": "confirm_ping", "id": request_id})

            approved = bool(entry["approved"])
            emit({
                "type": "confirm_result",
                "id": request_id,
                "approved": approved,
            })
            return approved
        finally:
            _drop_confirm(request_id)

    return confirm


# ─────────────────────────────────────────────
# HTTP 处理
# ─────────────────────────────────────────────

class AgentRequestHandler(BaseHTTPRequestHandler):
    server_version = "DayuAgent/1.0"
    protocol_version = "HTTP/1.1"

    # ── 基础工具 ──────────────────────────────

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError(f"请求体过大（>{MAX_BODY} 字节）")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename: str, content_type: str) -> None:
        path = WEB_ROOT / filename
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json(404, {"error": f"前端文件缺失：{path}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ── 路由 ──────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlsplit(self.path).path
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self.send_static(filename, content_type)
        elif path == "/api/info":
            self.send_json(200, {
                "model": mini_agent.MODEL,
                "base_url": mini_agent.BASE_URL or "",
                "workspace": str(mini_agent.WORKSPACE),
                "client": "anthropic SDK" if mini_agent.Anthropic is not None
                else "标准库兼容客户端",
                "has_credentials": bool(mini_agent.AUTH_TOKEN),
                "max_loop": mini_agent.MAX_LOOP,
            })
        else:
            self.send_json(404, {"error": "未找到"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        # 只接受同源、JSON 的请求：防止随便一个网页通过表单/简单请求
        # 偷偷驱动本机的 agent（localhost 服务常见的 CSRF 风险）。
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            self.send_json(403, {"error": "跨源请求被拒绝"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self.send_json(415, {"error": "Content-Type 必须是 application/json"})
            return

        path = urlsplit(self.path).path
        if path == "/api/chat":
            self.handle_chat()
        elif path == "/api/confirm":
            self.handle_confirm()
        elif path == "/api/reset":
            self.handle_reset()
        else:
            self.send_json(404, {"error": "未找到"})

    # ── 接口实现 ──────────────────────────────

    def handle_confirm(self) -> None:
        """回答一次"要不要执行"的询问。"""
        try:
            payload = self._json_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        request_id = str(payload.get("request_id") or "").strip()[:128]
        approved = bool(payload.get("approved"))
        found = resolve_confirm(request_id, approved)
        self.send_json(200, {"ok": found})

    def handle_reset(self) -> None:
        try:
            payload = self._json_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        session_id = str(payload.get("session_id") or "").strip()[:128]
        if session_id:
            reset_session(session_id)
        self.send_json(200, {"ok": True})

    def handle_chat(self) -> None:
        try:
            payload = self._json_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return

        message = str(payload.get("message") or "").strip()
        if not message:
            self.send_json(400, {"error": "消息不能为空"})
            return

        session_id = str(payload.get("session_id") or "").strip()[:128] or uuid.uuid4().hex
        session = get_session(session_id)

        # 先发响应头，之后逐步往这条连接里写 SSE 事件
        self.send_response(200)
        for name, value in SSE_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

        def emit(event: dict) -> None:
            """把 run_agent 的进度事件写成一条 SSE。

            浏览器断开时 write/flush 会抛 BrokenPipe，转成 ClientDisconnected
            一路抛回 run_agent，让本轮尽快停止。
            """
            data = json.dumps(event, ensure_ascii=False, default=str)
            try:
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise ClientDisconnected() from exc

        # 需要确认时：把询问推给前端，等用户点允许 / 拒绝
        confirm = None
        if getattr(self.server, "require_confirm", True):
            confirm = make_confirm_callback(emit)

        # 同一会话串行执行；快照用于浏览器断开时回滚半截对话
        with session.lock:
            snapshot = len(session.history)
            try:
                emit({"type": "session", "session_id": session_id})
                reply = mini_agent.run_agent(
                    message,
                    client=getattr(self.server, "agent_client", None),
                    verbose=False,
                    history=session.history,
                    on_event=emit,
                    on_confirm=confirm,
                )
            except ClientDisconnected:
                # 跑到一半断开：本轮历史不完整，回滚，别污染下一轮
                del session.history[snapshot:]
            except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要告诉前端
                del session.history[snapshot:]
                try:
                    emit({"type": "error", "message": f"服务端异常：{exc}"})
                    emit({"type": "done", "text": ""})
                except ClientDisconnected:
                    pass
            else:
                # 本轮已经跑完；前端此刻断开也不该丢历史
                try:
                    emit({"type": "done", "text": reply})
                except ClientDisconnected:
                    pass
            finally:
                self.close_connection = True


class AgentHTTPServer(ThreadingHTTPServer):
    """带 agent 客户端的 HTTP 服务；daemon 线程让 Ctrl-C 能立刻退出。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, client=None, require_confirm: bool = True):
        self.agent_client = client
        self.require_confirm = require_confirm
        super().__init__(address, AgentRequestHandler)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    client=None,
    require_confirm: bool = True,
) -> AgentHTTPServer:
    return AgentHTTPServer((host, port), client=client, require_confirm=require_confirm)


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="web_server.py",
        description="拙愚智能 浏览器对话框（标准库 HTTP + SSE，无第三方依赖）。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="不询问就执行写文件 / 命令（默认每次都问）",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    # 提前构造客户端，启动时就把配置问题暴露出来；失败也不阻止页面启动，
    # 每次提问会由 run_agent 把错误作为事件返回给前端。
    client = None
    try:
        client = mini_agent._build_client()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 模型客户端初始化失败：{exc}")
        print("       页面仍会启动，但提问会返回错误；请检查 .env 配置。")

    try:
        server = create_server(
            args.host, args.port, client=client,
            require_confirm=not args.no_confirm,
        )
    except OSError as exc:
        print(f"[ERROR] 无法监听 {args.host}:{args.port}：{exc}")
        return 1
    actual_port = server.server_address[1]
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    url = f"http://{display_host}:{actual_port}/"

    print()
    print("┌─ 拙愚智能 Web ─────────────────────────────────────")
    print(f"│ 地址   {url}")
    print(f"│ 模型   {mini_agent.MODEL}" + (f" @ {mini_agent.BASE_URL}" if mini_agent.BASE_URL else ""))
    print(f"│ 工作目录 {mini_agent.WORKSPACE}")
    print(f"│ 确认   {'每次写文件/执行命令都要你点允许' if not args.no_confirm else '已关闭（--no-confirm）'}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("│ [WARN] 当前监听地址不是本机回环，局域网内任何人都能操作这个 agent！")
    print("│ Ctrl-C 停止")
    print("└─────────────────────────────────────────────────────")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 无图形环境时忽略
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

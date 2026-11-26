/* 大愚智能 前端：原生 JS，无构建、无第三方依赖。
 * 通过 POST /api/chat 读取 SSE 流，实时渲染 agent 的每个处理步骤。
 */
(() => {
  "use strict";

  // ── DOM ────────────────────────────────────────────────
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const resetBtn = document.getElementById("reset");
  const statusEl = document.getElementById("status");
  const modelLine = document.getElementById("model-line");
  const workspaceEl = document.getElementById("workspace");

  const EMPTY_TEMPLATE = messagesEl.innerHTML;
  const SESSION_KEY = "dayu.session";
  const MAX_STEP_LABEL = 999;

  const TOOL_META = {
    read: { icon: "📄", label: "读取文件" },
    write: { icon: "✍️", label: "写入文件" },
    edit: { icon: "🩹", label: "编辑文件" },
    bash: { icon: "💻", label: "执行命令" },
  };

  const state = {
    busy: false,
    controller: null,
    sessionId: readSession(),
  };

  // ── 会话 id ────────────────────────────────────────────
  function readSession() {
    try {
      return localStorage.getItem(SESSION_KEY) || "";
    } catch {
      return "";
    }
  }

  function saveSession(id) {
    if (!id) return;
    state.sessionId = id;
    try {
      localStorage.setItem(SESSION_KEY, id);
    } catch {
      /* 隐私模式下 localStorage 不可用，忽略 */
    }
  }

  // ── 小工具 ─────────────────────────────────────────────
  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  // Markdown 渲染在独立文件 web/markdown.js 里，便于单独测试。
  // 如果它没加载成功，退化为「转义 + 换行」，保证内容可读且不会执行 HTML。
  const renderMarkdown =
    (window.FoolMarkdown && window.FoolMarkdown.renderMarkdown) ||
    ((text) =>
      String(text ?? "")
        .replace(/[&<>"']/g, (ch) => ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }[ch]))
        .replace(/\n/g, "<br>"));

  function truncate(text, limit = 600) {
    const str = String(text ?? "");
    if (str.length <= limit) return str;
    return `${str.slice(0, limit)}\n…（共 ${str.length} 字符）`;
  }

  /** 把工具参数整理成一眼能看懂的文本。 */
  function formatArgs(name, args) {
    if (!args || typeof args !== "object") return String(args ?? "");
    switch (name) {
      case "bash": {
        let out = `$ ${args.command ?? ""}`;
        if (args.timeout && args.timeout !== 60) out += `\n# timeout: ${args.timeout}s`;
        return out;
      }
      case "write":
        return `path: ${args.path ?? ""}\n\n${truncate(args.content, 800)}`;
      case "edit":
        return (
          `path: ${args.path ?? ""}\n\n` +
          `--- 原文 ---\n${truncate(args.old_text, 400)}\n\n` +
          `+++ 新文 +++\n${truncate(args.new_text, 400)}`
        );
      case "read": {
        const lines = [`path: ${args.path ?? ""}`];
        if (args.offset) lines.push(`offset: ${args.offset}`);
        if (args.limit && args.limit !== 2000) lines.push(`limit: ${args.limit}`);
        return lines.join("\n");
      }
      default:
        return JSON.stringify(args, null, 2);
    }
  }

  function looksLikeError(output) {
    const text = String(output ?? "");
    return /^错误[：:]/.test(text) || /\[exit code\]\s*[1-9]/.test(text);
  }

  // ── 滚动 ───────────────────────────────────────────────
  function nearBottom() {
    return (
      messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 160
    );
  }

  // 用户往上翻时暂停自动滚动；翻回底部后恢复。用持续状态而不是每次算，
  // 避免内容快速增高时“刚好不算在底部”导致再也不跟着滚。
  let stickToBottom = true;

  messagesEl.addEventListener(
    "scroll",
    () => {
      stickToBottom = nearBottom();
    },
    { passive: true }
  );

  function scrollToBottom(force = false) {
    if (force) stickToBottom = true;
    if (!stickToBottom) return;
    messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "auto" });
  }

  // ── 状态 ───────────────────────────────────────────────
  function setStatus(kind, text) {
    statusEl.className = `status ${kind}`;
    statusEl.textContent = text;
  }

  function setBusy(busy) {
    state.busy = busy;
    sendBtn.classList.toggle("stop", busy);
    sendBtn.textContent = busy ? "停止" : "发送";
    sendBtn.disabled = false;
    resetBtn.disabled = busy;
    statusEl.setAttribute("aria-busy", busy ? "true" : "false");
  }

  // ── 消息渲染 ───────────────────────────────────────────
  function removeEmptyState() {
    const empty = messagesEl.querySelector(".empty");
    if (empty) empty.remove();
  }

  function addUserMessage(text) {
    const row = el("div", "msg user");
    const bubble = el("div", "user-bubble");
    bubble.textContent = text;
    row.appendChild(bubble);
    messagesEl.appendChild(row);
  }

  function createAssistantTurn() {
    const root = el("article", "msg assistant");
    const avatar = el("div", "avatar");
    avatar.textContent = "F";
    avatar.setAttribute("aria-hidden", "true");

    const content = el("div", "content");

    const trace = el("div", "trace");
    trace.hidden = true;
    const traceHead = el("div", "trace-head");
    const traceTitle = el("span");
    traceTitle.textContent = "处理步骤";
    const traceCount = el("span", "trace-count");
    traceCount.textContent = "0 步";
    traceHead.append(traceTitle, traceCount);
    const steps = el("div", "steps");
    trace.append(traceHead, steps);

    const answer = el("div", "answer");

    const working = el("div", "working");
    const spinner = el("span", "spinner");
    const workingText = el("span");
    workingText.textContent = "Agent 正在处理…";
    working.append(spinner, workingText);

    content.append(trace, answer, working);
    root.append(avatar, content);

    const turn = {
      root,
      hasAnswer: false,
      hasError: false,
      finished: false,
      stepCount: 0,
      currentStep: null,
      tools: new Map(),

      addStep(number) {
        trace.hidden = false;
        this.stepCount += 1;
        traceCount.textContent = `${this.stepCount} 步`;
        if (this.currentStep) {
          this.currentStep.state.textContent = "完成";
          this.currentStep.box.classList.remove("active");
        }

        const box = el("div", "step active");
        const head = el("div", "step-head");
        const index = el("span", "step-index");
        index.textContent = Math.min(number || this.stepCount, MAX_STEP_LABEL);
        const label = el("span", "step-label");
        label.textContent = `第 ${number || this.stepCount} 步`;
        const stepState = el("span", "step-state");
        stepState.textContent = "进行中";
        head.append(index, label, stepState);
        const items = el("div", "step-items");
        box.append(head, items);
        steps.appendChild(box);

        this.currentStep = { box, items, state: stepState };
      },

      ensureStep() {
        if (!this.currentStep) this.addStep(this.stepCount + 1);
        return this.currentStep;
      },

      addThinking(text) {
        const step = this.ensureStep();
        const wrap = el("div", "item think");
        const details = el("details");
        const summary = el("summary");
        summary.textContent = `模型思考（${String(text).length} 字符）`;
        const body = el("div", "item-body");
        body.textContent = text;
        details.append(summary, body);
        wrap.appendChild(details);
        step.items.appendChild(wrap);
      },

      addNote(text) {
        const step = this.ensureStep();
        const note = el("div", "item note");
        note.innerHTML = renderMarkdown(text);
        step.items.appendChild(note);
      },

      addTool(id, name, args) {
        const step = this.ensureStep();
        const meta = TOOL_META[name] || { icon: "⚙️", label: "调用工具" };
        const box = el("div", "tool");
        const head = el("div", "tool-head");
        const icon = el("span", "tool-icon");
        icon.textContent = meta.icon;
        const label = el("span", "tool-label");
        label.textContent = meta.label;
        const badge = el("span", "tool-name");
        badge.textContent = name || "tool";
        const status = el("span", "tool-status");
        status.textContent = "运行中…";
        head.append(icon, label, badge, status);

        const argsEl = el("pre");
        argsEl.textContent = formatArgs(name, args);

        const resultBox = el("details", "tool-result");
        resultBox.hidden = true;
        const summary = el("summary");
        summary.textContent = "查看结果";
        const outputEl = el("pre");
        resultBox.append(summary, outputEl);

        box.append(head, argsEl, resultBox);
        step.items.appendChild(box);

        const key = id || `anon-${step.items.children.length}`;
        this.tools.set(key, { box, status, resultBox, summary, outputEl });
      },

      finishTool(id, name, output) {
        const key = id || "";
        let entry = this.tools.get(key);
        if (!entry) {
          // 极端情况下只收到了结果，补一张卡片，避免信息丢失
          this.addTool(id, name, {});
          entry = this.tools.get(key || `anon-${this.currentStep.items.children.length}`);
        }
        if (!entry) return;
        const text = String(output ?? "");
        const failed = looksLikeError(text);
        entry.status.textContent = failed ? "出错" : "完成";
        entry.box.classList.add(failed ? "error" : "done");
        entry.summary.textContent = `查看结果（${text.length} 字符）`;
        entry.outputEl.textContent = text || "（无输出）";
        entry.resultBox.hidden = false;
        if (failed) entry.resultBox.open = true;
      },

      addError(message) {
        this.hasError = true;
        const step = this.ensureStep();
        const box = el("div", "item error");
        box.textContent = String(message ?? "未知错误");
        step.items.appendChild(box);
      },

      setAnswer(text) {
        this.hasAnswer = true;
        answer.innerHTML = renderMarkdown(text);
      },

      finish(kind) {
        if (this.finished) return;
        this.finished = true;
        working.remove();
        root.classList.toggle("stopped", kind === "stopped");
        if (this.currentStep) {
          this.currentStep.state.textContent = kind === "stopped" ? "已停止" : "完成";
          this.currentStep.box.classList.remove("active");
        }
      },
    };

    return turn;
  }

  // ── SSE 读取 ───────────────────────────────────────────
  async function readEventStream(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");

      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const payload = raw
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!payload) continue;
        try {
          onEvent(JSON.parse(payload));
        } catch (err) {
          console.warn("无法解析 SSE 事件：", raw, err);
        }
      }
    }

    const tail = buffer.trim();
    if (tail.startsWith("data:")) {
      try {
        onEvent(JSON.parse(tail.replace(/^data:\s*/, "")));
      } catch (err) {
        console.warn("无法解析尾部 SSE 事件：", tail, err);
      }
    }
  }

  function handleEvent(ev, turn) {
    switch (ev.type) {
      case "session":
        saveSession(ev.session_id);
        break;
      case "step":
        turn.addStep(ev.step);
        break;
      case "thinking":
        turn.addThinking(ev.text || "");
        break;
      case "assistant_text":
        turn.addNote(ev.text || "");
        break;
      case "tool_call":
        turn.addTool(ev.id, ev.name, ev.input);
        break;
      case "tool_result":
        turn.finishTool(ev.id, ev.name, ev.output);
        break;
      case "error":
        turn.addError(ev.message);
        break;
      case "final":
        turn.setAnswer(ev.text || "");
        break;
      case "done":
        if (!turn.hasAnswer && !turn.hasError && ev.text) {
          turn.setAnswer(ev.text);
        }
        turn.finish("done");
        break;
      default:
        break;
    }
  }

  // ── 发送 ───────────────────────────────────────────────
  async function send(rawText) {
    if (state.busy) return;
    const text = String(rawText ?? inputEl.value).trim();
    if (!text) return;

    inputEl.value = "";
    resizeInput();
    removeEmptyState();
    addUserMessage(text);

    const turn = createAssistantTurn();
    messagesEl.appendChild(turn.root);
    scrollToBottom(true);

    state.controller = new AbortController();
    setBusy(true);
    setStatus("running", "处理中");

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: state.sessionId }),
        signal: state.controller.signal,
      });

      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          const data = await response.json();
          if (data && data.error) detail += `：${data.error}`;
        } catch {
          /* 非 JSON 错误体，保留状态码 */
        }
        throw new Error(detail);
      }
      if (!response.body) {
        throw new Error("当前浏览器不支持流式响应");
      }

      await readEventStream(response.body, (ev) => {
        handleEvent(ev, turn);
        scrollToBottom();
      });
      turn.finish("done");
    } catch (err) {
      if (err && err.name === "AbortError") {
        turn.addNote("已停止本轮。服务端会在下一次事件时中断并回滚本轮上下文。");
        turn.finish("stopped");
        setStatus("idle", "已停止");
      } else {
        turn.addError(`请求失败：${err && err.message ? err.message : err}`);
        turn.finish("error");
        setStatus("error", "出错");
      }
    } finally {
      state.controller = null;
      setBusy(false);
      if (turn.hasError) {
        setStatus("error", "出错");
      } else if (statusEl.textContent === "处理中") {
        setStatus("idle", "空闲");
      }
      scrollToBottom();
      inputEl.focus();
    }
  }

  // ── 重置 ───────────────────────────────────────────────
  async function resetChat() {
    if (state.busy) {
      state.controller?.abort();
    }
    const sessionId = state.sessionId;
    if (sessionId) {
      try {
        await fetch("/api/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId }),
        });
      } catch {
        /* 服务端不可达也照样清空本地界面 */
      }
    }
    state.sessionId = "";
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch {
      /* ignore */
    }
    messagesEl.innerHTML = EMPTY_TEMPLATE;
    setStatus("idle", "空闲");
    inputEl.focus();
  }

  // ── 配置信息 ───────────────────────────────────────────
  async function loadInfo() {
    try {
      const response = await fetch("/api/info", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const info = await response.json();
      const parts = [info.model || "未知模型", info.client || ""];
      if (!info.has_credentials) parts.push("未配置凭据");
      modelLine.textContent = parts.filter(Boolean).join(" · ");
      modelLine.title = info.base_url || "";
      workspaceEl.textContent = `工作目录：${info.workspace || "?"}`;
      workspaceEl.title = info.workspace || "";
    } catch (err) {
      modelLine.textContent = "配置读取失败";
      console.warn("读取 /api/info 失败：", err);
    }
  }

  // ── 输入框 ─────────────────────────────────────────────
  function resizeInput() {
    inputEl.style.height = "auto";
    inputEl.style.height = `${Math.min(inputEl.scrollHeight, 190)}px`;
  }

  inputEl.addEventListener("input", resizeInput);

  inputEl.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!state.busy) send();
    }
  });

  sendBtn.addEventListener("click", () => {
    if (state.busy) {
      state.controller?.abort();
      return;
    }
    send();
  });

  resetBtn.addEventListener("click", resetChat);

  // 空状态的建议问题用事件委托，重置后依然有效
  messagesEl.addEventListener("click", (event) => {
    const button = event.target.closest(".suggestion");
    if (button && !state.busy) {
      send(button.textContent.trim());
    }
  });

  // ── 启动 ───────────────────────────────────────────────
  loadInfo();
  resizeInput();
  inputEl.focus();
})();

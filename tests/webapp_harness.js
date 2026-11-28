/* webapp_harness.js — 给 web/app.js 套一层最小 DOM，在 node 里跑它的渲染逻辑。
 *
 * 用法：node tests/webapp_harness.js <app.js 路径> <事件 JSON 数组>
 * 输出：渲染完成后的界面状态（JSON），供 Python 测试断言。
 */
"use strict";

const fs = require("fs");
const path = require("path");

// ── 最小 DOM ────────────────────────────────────────────────

class FakeClassList {
  constructor(node) {
    this.node = node;
    this.set = new Set();
  }
  add(...names) { names.forEach((n) => this.set.add(n)); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); }
  contains(name) { return this.set.has(name); }
  toggle(name, force) {
    const want = force === undefined ? !this.set.has(name) : !!force;
    if (want) this.set.add(name);
    else this.set.delete(name);
    return want;
  }
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._classes = new Set();
    this._text = "";
    this._html = "";
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.attributes = {};
    this.listeners = {};
    this.classList = new FakeClassList(this);
  }

  get className() { return [...this._classes].join(" "); }
  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    // 浏览器里 innerHTML 会生成子节点，这里不会，就从 HTML 里扒出纯文本
    if (this._html) return this._html.replace(/<[^>]*>/g, "");
    return this._text;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._html = String(value);
    this.children = [];
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  remove() {
    const parent = this.parentNode;
    if (!parent) return;
    const index = parent.children.indexOf(this);
    if (index >= 0) parent.children.splice(index, 1);
    this.parentNode = null;
  }

  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  removeEventListener() {}
  click() {
    (this.listeners.click || []).forEach((fn) => fn({ target: this }));
  }
  focus() {}
  scrollTo() {}
  get scrollHeight() { return 0; }
  get scrollTop() { return 0; }
  set scrollTop(_value) {}
  get clientHeight() { return 0; }

  _matches(selector) {
    if (!selector.startsWith(".")) return this.tagName === selector.toUpperCase();
    // 支持 ".a.b" 这种多类选择器
    return selector.slice(1).split(".").every((name) => this._classes.has(name));
  }

  _walk(fn) {
    for (const child of this.children) {
      fn(child);
      child._walk(fn);
    }
  }

  querySelector(selector) {
    let found = null;
    this._walk((node) => { if (!found && node._matches(selector)) found = node; });
    return found;
  }

  querySelectorAll(selector) {
    const out = [];
    this._walk((node) => { if (node._matches(selector)) out.push(node); });
    return out;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node._matches && node._matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}

function buildDocument(ids) {
  const registry = {};
  for (const id of ids) {
    const node = new FakeElement("div");
    node.id = id;
    registry[id] = node;
  }
  return {
    registry,
    getElementById: (id) => registry[id] || null,
    createElement: (tag) => new FakeElement(tag),
  };
}

// ── 加载 app.js ─────────────────────────────────────────────

const appPath = path.resolve(process.argv[2]);
const events = JSON.parse(process.argv[3] || "[]");

const document = buildDocument([
  "messages", "input", "send", "reset", "status", "model-line", "workspace",
]);

const localStorage = {
  _data: {},
  getItem(key) { return this._data[key] ?? null; },
  setItem(key, value) { this._data[key] = String(value); },
  removeItem(key) { delete this._data[key]; },
};

const fakeWindow = {
  FoolMarkdown: require(path.join(path.dirname(appPath), "markdown.js")),
};

// app.js 是 IIFE，把内部函数暴露出来给测试用（只改这一处，源码本身不动）
let source = fs.readFileSync(appPath, "utf8");
const patched = source.replace(
  /\}\)\(\);\s*$/,
  "window.__test = { handleEvent, createAssistantTurn };\n})();"
);
if (patched === source) {
  console.error("注入失败：没找到 app.js 结尾的 })();");
  process.exit(2);
}

// app.js 启动时会拉一次 /api/info，给个假的成功响应，免得刷错误日志
const fakeFetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve({
    model: "test-model",
    client: "test-client",
    workspace: "/tmp",
    base_url: "",
  }),
});

const factory = new Function("window", "document", "localStorage", "fetch", patched);
factory(fakeWindow, document, localStorage, fakeFetch);

// ── 跑事件并收集界面状态 ───────────────────────────────────

const turn = fakeWindow.__test.createAssistantTurn();
document.registry.messages.appendChild(turn.root);

for (const event of events) {
  // "__click" 是测试自己用的指令，不喂给 app.js
  if (event.type === "__click") {
    const entry = turn.confirms.get(event.id);
    if (!entry) throw new Error(`没有这个确认卡片：${event.id}`);
    if (event.approved) entry.allow.click();
    else entry.deny.click();
    continue;
  }
  fakeWindow.__test.handleEvent(event, turn);
  // 增量渲染是节流（50ms）的，这里同步冲掉定时器
  if (turn.renderTimer) {
    clearTimeout(turn.renderTimer);
    turn.renderTimer = null;
    turn.renderAnswer();
  }
}

const root = turn.root;
const answer = root.querySelector(".answer");

const summary = {
  answerHtml: answer ? answer.innerHTML : "",
  notes: root.querySelectorAll(".item.note").map((n) => n.textContent),
  thinking: root.querySelectorAll(".item.think").map((n) => n.textContent),
  tools: root.querySelectorAll(".tool").map((t) => ({
    label: (t.querySelector(".tool-label") || { textContent: "" }).textContent,
    name: (t.querySelector(".tool-name") || { textContent: "" }).textContent,
    status: (t.querySelector(".tool-status") || { textContent: "" }).textContent,
  })),
  confirms: root.querySelectorAll(".confirm").map((c) => ({
    buttons: c.querySelectorAll("button").map((b) => b.textContent),
    status: (c.querySelector(".confirm-status") || { textContent: "" }).textContent,
    desc: (c.querySelector(".confirm-desc") || { textContent: "" }).textContent,
  })),
  errors: root.querySelectorAll(".item.error").map((n) => n.textContent),
  steps: root.querySelectorAll(".step").length,
  stepLabels: root.querySelectorAll(".step-label").map((n) => n.textContent),
};

process.stdout.write(JSON.stringify(summary));

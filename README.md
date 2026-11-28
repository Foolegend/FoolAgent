# 拙愚智能

基于 Pi 哲学的最小 Agent Loop：**四个原子工具 + 一个 while 循环，不做任何框架抽象**。

> 设计原则照搬 Pi：工具永远只有 4 个，能力扩展靠 `bash` 调 CLI，而不是不断加工具。

## 快速开始

模型配置从 `.env` 读取（也可直接用环境变量覆盖）：

```bash
# .env（已被 .gitignore 忽略，模板见 .env.example）
INVEST_ANTHROPIC_BASE_URL=https://api.minimax.cn/anthropic
INVEST_ANTHROPIC_AUTH_TOKEN=sk-xxx
INVEST_ANTHROPIC_MODEL=MiniMax-M3.1-Flash
```

```bash
python src/mini_agent.py              # 交互模式：一直问，一直答，上下文自动保留
python src/mini_agent.py "你的任务"    # 单次任务：跑完就退出
```

交互模式下：

```
你 > 这个项目是干什么的
──────────────────────────────────────────────────
[Step 1] 拙愚智能
这是一个基于 Pi 哲学的最小 Agent Loop 项目……   # 边生成边显示

你 > 那 run.py 里的 chat 命令做什么
──────────────────────────────────────────────────
[Step 1] 拙愚智能
`run chat` 启动交互式对话……                    # 模型看得到上一轮对话
你 > /clear         # 清空上下文，从零开始
你 > /yes           # 本会话内自动允许写文件 / 执行命令
你 > /ask           # 恢复逐次询问（默认）
你 > /exit          # 退出（Ctrl-D 同）
```

启动时会打印实际生效的配置：

```
工作目录：D:\project\github\FoolAgent
模式：交互对话
模型：MiniMax-M3.1-Flash @ https://api.minimax.cn/anthropic（标准库兼容客户端）
```

## 流式输出

模型每生成一段，终端就立刻显示一段，不用等整轮生成完：

```
[Step 1] 拙愚智能
我先看看当前目录有什么文件。
  → bash({"command": "ls -la"})
  ← total 1092 drwxr-xr-x ...
  → write({"path": "hello.py", "content": "print('hi')\n"})
  ← 已写入 hello.py（25 字符）

[Step 2] 拙愚智能
文件写好了，运行结果是 hi。
```

实现上有两条路径，都走同一套增量事件：

| 客户端 | 怎么拿流 | 说明 |
|--------|----------|------|
| 官方 `anthropic` SDK | `messages.create(stream=True)` | 直接拿原始 SSE 事件 |
| 内置标准库客户端 | 自己解析 SSE | 同一套协议，不引第三方依赖 |

服务端不收 `stream` 参数时（部分兼容网关会返回 400），自动退回一次性调用，
整轮不会失败。两条路径最后都会把内容拼回**和一次性调用完全一样**的
`content` 块（含 `tool_use` 的完整参数），所以对话历史不受影响。

### 虚拟环境（推荐）

项目只依赖标准库就能跑；只有想用官方 `anthropic` SDK 时才需要装包。装了包就放进虚拟环境：

```bash
python -m venv .venv
# cmd
.venv\Scripts\activate.bat
# PowerShell
.venv\Scripts\Activate.ps1
# Git Bash
source .venv/Scripts/activate

pip install -r requirements.txt
```

`run.cmd` / `run.sh` 会自动优先使用 `.venv` 里的解释器；直接 `python src/mini_agent.py`
时则取决于当前终端激活的是哪个环境。

### 配置读取顺序

`.env` 的查找顺序是 `ANTHROPIC_ENV_FILE` > 当前目录 > 仓库根目录；
**环境变量优先级高于 `.env`**，所以 CI 里直接 `export INVEST_ANTHROPIC_MODEL=...` 就能覆盖。

支持的键（`INVEST_` 前缀优先，其次兼容标准 Anthropic 变量名）：

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `INVEST_ANTHROPIC_BASE_URL` / `ANTHROPIC_BASE_URL` | 兼容网关地址 | 无（不设置则走官方 API） |
| `INVEST_ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` | 鉴权 token | 无 |
| `INVEST_ANTHROPIC_MODEL` / `INVEST_ANTHROPIC_DEFAULT_SONNET_MODEL` | 模型 ID | `claude-sonnet-4-5` |

### 客户端

- 装了 `anthropic` SDK 就用 SDK（`pip install -r requirements.txt`）。
- **没装也能跑**：内置 `AnthropicCompatClient` 用标准库 `urllib` 走同一个
  Anthropic Messages API，接口完全一致。启动信息里会显示当前用的是哪种。
- 两种客户端都支持流式（见下节），流式行为一致。

工作目录 = 当前目录，所有文件操作和命令都被限制在该目录内。

## 技能：`.fool/skills/`

技能包就是一个目录，里面有 `SKILL.md`（说明）和可选的 `scripts/`、`references/`：

```
.fool/skills/<名字>/
  SKILL.md            # 头部 frontmatter 写 name / description
  scripts/...         # 可以直接跑的脚本
  references/...      # 参考资料
```

启动时（准确说是每次调模型时）扫一遍工作区里的 `.fool/skills/`，
把技能索引拼进 system prompt：

```
可用技能（从工作区的技能包里发现的）：
- fibonacci-bignum：计算大整数斐波那契数列与前 N 项之和……
  说明文件：.fool/skills/fibonacci-bignum/SKILL.md
  可以直接跑的脚本：.fool/skills/fibonacci-bignum/scripts/fibonacci.py
  参考资料：.fool/skills/fibonacci-bignum/references/bignum-add.md
需要某个技能时，先 read 它的说明文件，再按说明用 bash 调它的脚本；不要自己另写一份。
```

**工具数量没有变**，还是 read / write / edit / bash 四个——模型要跑技能脚本，
照旧用 `bash` 调。这正是文档里说的「能力扩展靠 bash 调 CLI，而不是不断加工具」。

放了新技能包不用重启，下次提问就能用上。

## 操作确认

`write` / `edit` / `bash` 有副作用，执行前会先问用户。`read` 是只读的，不问。

终端里：

```
※ 需要确认：执行命令：python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 50
  允许执行吗？[y/N]
```

- 交互式终端：逐次询问，回 `y` 允许，其它一律拒绝；
  拒绝后不会执行，模型会收到「用户拒绝执行」，让它换个做法而不是重试。
- `/yes` 切换成本会话自动允许，`/ask` 切回来。
- 非交互终端（管道、脚本）没法问人，自动放行并打印一次说明。

网页版会弹一张确认卡片，点「允许 / 拒绝」：

```
python src/web_server.py                    # 默认每次都问
python src/web_server.py --no-confirm       # 不问，直接执行
```

## 浏览器对话框

不想待在终端里时，可以启动一个本地网页版对话界面：

```bash
python src/web_server.py                 # 启动后自动打开 http://127.0.0.1:8765
python src/web_server.py --port 9000 --no-browser
./run.sh web                             # 等价入口
```

在输入框输入问题后，回答区会实时展示：

- 用户消息与最终回答（支持 Markdown 标题、列表、任务列表、表格、引用、代码块、链接等）；
- 模型**正在生成的文字**，逐段出现在回答区（不是等整轮生成完才显示）；
- 每一轮模型的思考块和过渡性说明；
- 每次工具调用的名称、参数、执行结果（结果默认折叠，点开可看完整输出）；
- 写文件 / 执行命令前的**确认卡片**，点「允许 / 拒绝」；
- 出错时的红色提示；运行中可点「停止」中断本轮。

实现只用标准库，没有 Web 框架、没有前端构建步骤，也没有 CDN 依赖：

| 文件 | 作用 |
|------|------|
| `src/web_server.py` | HTTP 服务 + SSE 流式推送 |
| `web/index.html` · `web/style.css` · `web/app.js` | 原生前端 |
| `web/yugong.svg` | 标识图：愚公移山（手绘 SVG，无二进制资源） |
| `web/markdown.js` | 轻量 Markdown 渲染器（无第三方依赖，可被 Node 直接测试） |
| `run_agent(..., on_event=...)` | 把 agent loop 的每一步转成结构化事件 |

事件契约：

| 事件 | 何时发 | 载荷 |
|------|--------|------|
| `step` | 每轮开始 | `step` |
| `text_delta` | 流式模式下，模型每吐出一段文字 | `text` |
| `thinking_delta` | 流式模式下，模型每吐出一段思考 | `text` |
| `thinking` | 非流式客户端，整块思考 | `text` |
| `assistant_text` | 非流式客户端，调用工具前的过渡性文字 | `text` |
| `tool_call` / `tool_result` | 工具调用与结果 | `id` `name` `input` / `output` |
| `confirm` / `confirm_result` | 要用户点头才能执行 | `id` `name` `input` / `approved` |
| `tool_denied` | 用户拒绝了这次操作 | `id` `name` |
| `final` / `error` / `done` | 本轮结束 | `text` / `message` |

接口：

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/` | 前端页面 |
| `GET` | `/api/info` | 模型、工作目录等配置 |
| `POST` | `/api/chat` | 入参 `{"message", "session_id"}`，返回 `text/event-stream` 事件流 |
| `POST` | `/api/confirm` | 入参 `{"request_id", "approved"}`，回答一次确认询问 |
| `POST` | `/api/reset` | 清空某个会话的上下文 |

`session_id` 由前端生成并保存在 `localStorage`，刷新页面后仍能继续多轮对话；
点「清空对话」会同时清掉服务端历史。不同会话之间互不干扰，同一会话的并发请求会串行执行。

> ⚠️ 网页版把拥有文件读写和命令执行能力的 agent 暴露成了 HTTP 接口。
> 默认只绑定 `127.0.0.1`，不要在没有额外鉴权和容器隔离的情况下改成 `0.0.0.0`。

## 启动脚本

一条命令验证整条链路（环境 → 测试 → 真实 API → 端到端）：

```bash
run.cmd                       # Windows：双击即可
./run.sh                      # Linux / macOS
python run.py                 # 跨平台
```

| 命令 | 作用 |
|------|------|
| `run` | 全套四步，失败会打印原因并返回非 0 退出码 |
| `run check` | 只做环境自检（Python 版本、`.env`、凭据、依赖） |
| `run test` | 只跑单元测试（不需要 API key） |
| `run live` | 只验证真实 API 连通性（发一个最小请求） |
| `run agent` | 端到端：让真实模型在临时目录里建文件并跑起来 |
| `run agent "你的任务"` | 端到端，自定义任务（在当前目录执行） |
| `run chat` | 交互式对话，等价于 `python src/mini_agent.py`（`/exit` 退出） |
| `run web` | 浏览器对话框，实时显示每个处理步骤（Ctrl-C 退出） |

```
── 结果汇总 ────────────────────────────────────────────────────
  [OK] 环境自检
  [OK] 单元测试
  [OK] 真实 API
  [OK] 端到端

全部通过（4/4）—— 可以用了。
```

## 四个原子工具

| 工具 | 作用 | 关键约束 |
|------|------|----------|
| `read` | 读文件，返回带行号的文本 | 大文件用 `offset`/`limit` 分页；路径不得越界 |
| `write` | 创建或覆盖写文件 | 自动建父目录；路径不得越界 |
| `edit` | 精确替换一段文本 | `old_text` 必须**唯一**出现，否则报错要求更长上下文 |
| `bash` | 执行 shell 命令 | 命令白名单 + 路径不得越界 |

**为什么 `edit` 要求唯一**：允许多处匹配时模型极易改错地方。强制唯一 = 强制模型先 `read` 看清再动手。报错信息会直接告诉它「请提供更长的上下文」，下一轮它会自己修正。

## 沙箱

`bash` 拿到 shell 权限很危险，这里做了五层限制：

1. **白名单**：只允许 `python`/`git`/`ls`/`grep`/`npm`/`node` 等固定命令。
2. **逐段校验**：按 `&&`、`||`、`;`、`|`、换行切分后**每一段**都要校验，
   否则 `echo ok && rm -rf /` 会靠首段蒙混过关。
3. **命令替换禁用**：`$(...)` 和反引号能藏住任意命令，一律拒绝。
4. **路径限制**：参数里的绝对路径、`../`、`~`、`%VAR%` 一律拒绝；
   **选项值里的路径也查**，`--output=/tmp/x`、`--git-dir=/tmp/y` 这种
   藏在等号后面的同样拒绝。
5. **危险参数禁用**：`find -exec`、`git -c`、`git config --global` 这类
   能绕开白名单执行别的程序的一律拒绝。

生产环境建议再套一层 Docker：

```bash
docker run --rm -it \
  -v $(pwd):/workspace \
  --network=none \
  --memory=2g \
  --cpus=2 \
  python:3.11-slim \
  python /workspace/src/mini_agent.py "你的任务"
```

`--network=none` 断网，防止它偷偷访问外网。

## 代码结构

```
src/mini_agent.py       Agent 核心：四个原子工具 + 沙箱 + Agent Loop + 流式输出 + 操作确认
src/skills.py           技能发现：扫描 .fool/skills/，生成给模型看的索引
src/web_server.py       浏览器对话框后端：标准库 HTTP + SSE 进度流 + 确认往返
web/
  index.html            对话框页面
  style.css             样式（深色主题、移动端适配）
  app.js                原生 JS：SSE 解析、步骤时间线、流式渲染、确认卡片、会话管理
  markdown.js           轻量 Markdown 渲染器（标题/列表/表格/引用/任务列表等）
  yugong.svg            标识图：愚公移山（深色版，用于渐变底板）
  yugong-light.svg      标识图：愚公移山（浅色版，用于深色背景）
tests/
  test_tools.py         工具行为与沙箱边界
  test_agent.py         agent loop（注入假 client，不需要 API key）
  test_stream.py        流式输出：SSE 解析、增量拼装、事件契约
  test_skills.py        技能发现：frontmatter 解析、索引、拼进 system prompt
  test_confirm.py       操作确认：核心逻辑、终端提示、网页确认往返
  test_web.py           on_event 事件契约 + HTTP/SSE 接口
  test_webapp.py        前端渲染（node + 最小 DOM，缺 node 时跳过）
  test_markdown.py      Markdown 渲染器测试（调用 node，缺 node 时跳过）
  test_config.py        .env 解析与客户端构造
  test_live.py          真实 API 集成测试（需要凭据，默认跳过）
  webapp_harness.js     给 web/app.js 套的最小 DOM（配合 test_webapp.py）
.fool/skills/fibonacci-bignum/  技能包：大整数斐波那契（原 Java 实现的唯一存活形态，见文末）
```

实现严格对照 [`agent-design-plan.md`](./agent-design-plan.md) 的代码骨架：
函数名（`tool_read`/`tool_write`/`tool_edit`/`tool_bash`/`run_agent`）、
常量（`TOOLS`/`TOOL_IMPL`/`SYSTEM_PROMPT`/`MAX_LOOP`/`WORKSPACE`）、
schema 和系统提示词全部保持原样。

### 与设计文档的差异

只有几处**必要的**改动，其余逐行对应：

| 改动 | 原因 |
|------|------|
| `run_agent(user_input, client=None, verbose=True, history=None, on_event=None, on_text=None, on_confirm=None)` | 注入 `client` 便于离线测试；`history` 支持多轮；`on_event` 把每一步转成结构化事件供网页实时展示；`on_text` 让终端边生成边显示；`on_confirm` 让副作用操作先问过用户 |
| `from anthropic import Anthropic` 改成 try/except | 没装 SDK 时仍可 import 本模块跑测试，改用内置标准库客户端 |
| `MODEL`/`BASE_URL`/`AUTH_TOKEN` 从 `.env` 读取 | 原本硬编码 `claude-sonnet-4-5` + 读 `ANTHROPIC_API_KEY`，无法指向兼容网关 |
| 模型调用改成流式（`create(stream=True)`） | 文档 P0 提到「流式输出」，原示例代码是一次性返回，用户要干等整轮生成完 |
| 内置标准库客户端自己解析 SSE | 官方 SDK 没装时也要能流式，不能只有装 SDK 才有流式 |
| 新增 `src/skills.py`：扫描 `.fool/skills/` 生成技能索引 | 文档说「能力扩展靠 bash 调 CLI」，但没说 CLI 从哪来——索引让模型知道有哪些现成技能可用，工具数量不变 |
| 新增 `on_confirm` 钩子 | 文档 P1 的「工具调用确认」；写文件 / 执行命令之前先问，拒绝就不执行 |

另有 4 处是**修 bug**（文档的约束表写了要求，但示例代码没实现或实现错了）：

1. **超时杀不干净**：文档版 `subprocess.run(timeout=)` 只杀 `cmd.exe`，
   子进程 `python` 仍持有管道，函数会一直阻塞到子进程结束——实测超时 1 秒却卡了 30 秒。
   改为按进程组终止整棵进程树（Windows 用 `taskkill /T`）。
2. **白名单只看整条命令的第一段**：`echo ok && rm -rf /` 靠首段蒙混过关。
   改为按 `&&`/`||`/`;`/`|`/换行切分后逐段校验。
3. **路径越界判断有误**：文档用 `str(p).startswith(str(WORKSPACE))`，
   会把 `C:\workspace2` 误判成 `C:\workspace` 的子目录。
4. **约束表承诺的 bash 路径限制没实现**：文档代码里 `cat /etc/passwd` 是放行的。
   已补上：绝对路径、`../`、`~`、`%VAR%` 一律拒绝；命令替换 `$(...)`、反引号、
   `python -c` 内联执行同样拒绝。

## 设计取舍

- **不做上下文压缩**：超过阈值直接报错让人手动处理。自动摘要会丢信息，
  而丢的往往正是最需要的（比如某个函数签名）。
- **系统提示词保持极简**：Pi 的全部提示词不到 1000 tokens。实测提示词越长，
  模型越容易忽略关键约束，所以只保留 5 条核心工作原则。
- **不做框架抽象**：只依赖 `anthropic` SDK + 标准库，不引 LangChain，不做 MCP。
  连 `.env` 解析都是标准库手写的 15 行，不引 `python-dotenv`。
  需要外部能力就写 CLI，Agent 通过 `bash` 调用：

  ```bash
  quant-tools fetch-financials --code 600519 --year 2025
  quant-tools calc-position --capital 100000 --risk 0.02
  ```

  工具数量永远是 4 个，能力无限扩展。

## 测试

不需要 API key，模型调用被注入成可替换对象：

```bash
python -m unittest discover -s tests -v
```

真实 API 集成测试需要凭据（读 `.env` 里的 `INVEST_ANTHROPIC_AUTH_TOKEN` /
`INVEST_ANTHROPIC_BASE_URL` / `INVEST_ANTHROPIC_MODEL`），没配置就自动跳过：

```bash
python -m unittest tests.test_live -v
```

## 已知限制

- `bash` 白名单是**第一道防线**，不是强隔离。`python script.py` 能执行任意逻辑
  （脚本是 Agent 自己写的），`git`、`npm` 同样如此。
  真正的隔离要靠 Docker 或系统级沙箱。
- 单次 bash 输出上限 20000 字符，超出截断；上下文超过 600000 字符直接报错。
- 部分兼容网关不支持 `stream` 参数（返回 400），这时会自动退回一次性调用：
  功能不受影响，但终端上要等整轮生成完才一次性显示。

---

## 技能包：`.fool/skills/fibonacci-bignum/`

仓库里原有的 Java fibonacci 实现（`src/main/java/com/zte/lgf`：`Num` 大数加法 +
`Main` 斐波那契递推）已改造为技能包，唯一存活形态是 Python 实现：

```
.fool/skills/fibonacci-bignum/
  SKILL.md                  # 技能说明：何时用、参数、输出格式
  scripts/
    fibonacci.py            # 实现（单文件，只用标准库）
  references/
    bignum-add.md           # 算法原理、原代码到新代码的映射、行为差异、性能实测
```

```bash
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 20           # 输出 F(1)..F(20)，格式与原 Java 版逐字节一致
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 1000 --sum   # 前 1000 项之和
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py --selftest   # 自检 + 双引擎交叉验证
```

有两种计算引擎：`native` 用 Python 内置任意精度 `int`（默认），
`manual` 用移植过来的字符串大数加法（原 Java `Num.add` 的算法），两者输出永远一致。
详见 [`.fool/skills/fibonacci-bignum/SKILL.md`](./.fool/skills/fibonacci-bignum/SKILL.md)。

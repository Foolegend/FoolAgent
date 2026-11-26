---
name: fibonacci-bignum
description: 计算大整数斐波那契数列与前 N 项之和（十进制精确值，可达数十万位）。当需要 Fibonacci 数、F(n) 精确值、斐波那契数列求和，或需要超出 64 位精度的大整数加法时使用。关键词：斐波那契、Fibonacci、大数、高精度、big number、大整数加法。
---

# 大整数斐波那契（fibonacci-bignum）

本仓库原有的 Java 实现 `src/main/java/com/zte/lgf`（`Num` 大数加法 + `Main` 斐波那契递推）
已按要求删除，唯一存活形态就是这个 Python 技能包：自带实现 + 一键运行脚本 + 算法说明。

技能目录自带全部内容，不依赖 maven 工程，也不依赖仓库里的其它脚本。

## 何时使用

- 要 F(1)..F(N) 的精确十进制值。F(93) 就超出 `long` 精度，本实现没有这个上限。
- 要前 N 项之和：`sum(F(1..N)) = F(N+2) - 1`。
- 要一个「高精度十进制整数加法」原语（`Num.add`），可单独复用。
- 要验证大数加法实现是否正确：`--selftest` 会跑恒等式、边界用例，
  并把手写实现与 Python 原生任意精度 `int` 交叉验证。

**不适用**：需要负数、小数或其它进制的场合——本实现只支持非负十进制整数。

## 环境要求

Python 3.7+，**只用标准库，没有任何第三方依赖**，不需要装任何包，
也不需要激活虚拟环境。

## 快速使用

技能就是单个脚本 `.fool/skills/fibonacci-bignum/scripts/fibonacci.py`，直接用 Python 调用：

```bash
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py                    # 输出 F(1)..F(800)，格式与原 Java 版一致
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 20                 # 输出 F(1)..F(20)
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 1000 --sum         # 前 1000 项之和
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 50 --engine manual # 用手写的大数加法（原 Java 实现的算法）
python .fool/skills/fibonacci-bignum/scripts/fibonacci.py --selftest         # 自检
```

Linux / macOS 下也可以直接执行（文件带 `#!/usr/bin/env python3` 且已加执行位）：

```bash
./.fool/skills/fibonacci-bignum/scripts/fibonacci.py 20
```

## 参数

| 参数 | 含义 |
|------|------|
| `maxIndex` | 计算到第几项，默认 `800`（与原 `Main.java` 一致） |
| `--sum` | 只输出前 maxIndex 项之和，不输出数列 |
| `--selftest` | 跑自检，不输出数列；全部通过退出码 0，否则 1 |
| `--out FILE` | 输出写到文件而不是标准输出（UTF-8） |
| `--engine native\|manual` | 计算引擎，见下 |
| `--help` | 用法说明 |

## 两种引擎

| 引擎 | 实现 | 说明 |
|------|------|------|
| `native`（默认） | Python 内置 `int` | Python 的 int 本身就是任意精度，直接用 |
| `manual` | `Num` 字符串大数加法 | 原 Java `Num.add` 的移植版，保留「手写高精度」这件事本身 |

两种引擎输出**永远一致**，`--selftest` 会交叉验证（n=1..300 逐项比对，
并比对求和结果）。`manual` 存在的意义是保留原仓库那段手写大数加法的算法，
而不是为了性能。

## 输出格式

数列模式，每行一项，格式与原 Java `Main.java` **逐字节一致**（`下标=值`，平台行尾符）：

```
1=1
2=1
3=2
4=3
5=5
...
800=69283081864224717136290077681328518273399124385204820718966040597691435587278383112277161967532530675374170857404743017623467220361778016172106855838975759985190398725
```

求和模式：

```
sum(F(1..100)) = 927372692193078999175
```

## 数值约定

- 编号从 1 开始：`F(1)=1, F(2)=1, F(3)=2, F(4)=3, F(5)=5, ...`（与原实现一致）。
- 求和恒等式：`F(1)+...+F(N) = F(N+2) - 1`。

## 参考资料

- `references/bignum-add.md` — 大数加法的算法原理、原 Java 代码到 Python 技能代码的
  逐项映射、行为差异（去前导零、负数处理）、复杂度与性能实测、原 `NumTest` 的对照结果。

## 让 Claude Code 自动发现这个技能

Claude Code 只扫描 `.claude/skills/<名字>/`，所以把这个目录挂过去即可（Windows 用目录联接）：

```bash
# Windows（管理员或开发者模式均可）
mkdir .claude\skills
mklink /J .claude\skills\fibonacci-bignum .fool\skills\fibonacci-bignum

# Linux / macOS
mkdir -p .claude/skills
ln -s ../../.fool/skills/fibonacci-bignum .claude/skills/fibonacci-bignum
```

联接之后 `/fibonacci-bignum` 就能被触发。技能目录里的相对路径引用（`scripts/`、`references/`）
在联接下同样可用。

## 与仓库其他部分的关系

- 本仓库的 `src/mini_agent.py` 里，agent 的 `bash` 白名单**包含 `python`**，
  所以 拙愚智能 可以直接跑这个技能，无需改动白名单：

  ```bash
  python .fool/skills/fibonacci-bignum/scripts/fibonacci.py 20 --sum
  ```

  注意沙箱限制：路径参数必须是相对沙箱根目录（仓库根）的相对路径，
  绝对路径、`../`、`~` 都会被拒；`python -c` 内联执行也被禁，
  所以只能跑脚本文件。

#!/usr/bin/env python3
"""fibonacci.py — 大整数斐波那契数列（技能实现，Python 版）

由本仓库原有的 Java 实现 ``src/main/java/com/zte/lgf`` 移植而来
（那两个 Java 类已按要求删除，这份是它们唯一的存活形态）：

    Num.add(...)     →  Num.add(...)      大数（十进制字符串）加法
    Main.fibonacci   →  fibonacci(...)     斐波那契递推，F(1)=1, F(2)=1, ...

两种引擎，输出永远一致：

    native  用 Python 内置 int（本身就是任意精度），快，默认
    manual  用移植过来的字符串大数加法，也就是原 Java 的核心算法

``--selftest`` 会把两种引擎交叉验证，确保手写大数实现没有偏差。

用法：
    python fibonacci.py [maxIndex] [--sum] [--selftest] [--out FILE]
                        [--engine native|manual]

只依赖标准库，Python 3.7+ 可直接运行（无第三方依赖）。
"""

from __future__ import annotations

import sys

DEFAULT_MAX_INDEX = 800
ENGINE_NATIVE = "native"
ENGINE_MANUAL = "manual"


# ─────────────────────────────────────────────
# 大整数：十进制字符串加法（原 Java Num 的移植）
# ─────────────────────────────────────────────

class Num:
    """大整数：十进制数字串，低位在前（"321" 表示 123）。空串表示 0。

    只支持非负数——原 Java 版也没做负数支持（它会把 "-" 当数字位处理，
    结果是错的），这里改成显式拒绝。

    内部用低位在前的表示法：加法结果直接往队尾 append，单次加法 O(n)；
    原 Java 版是高位在前 + 往队首 insert(0, ...)，单次加法 O(n^2)。
    """

    __slots__ = ("digits",)

    def __init__(self, digits: str = "") -> None:
        self.digits = digits

    # ── 构造 ──
    @classmethod
    def zero(cls) -> "Num":
        return cls("")

    @classmethod
    def from_decimal(cls, text: str) -> "Num":
        """由高精度十进制字符串构造（高位在前）。"""
        text = (text or "").strip()
        if not text:
            return cls.zero()
        if text.startswith("+"):
            text = text[1:]
        if not text.isdigit():
            raise ValueError("不是非负十进制整数：%s" % text)
        reversed_digits = text[::-1]
        # 去掉低位在前的表示里的前导零（末尾的 0）
        stripped = reversed_digits.rstrip("0")
        return cls(stripped)

    @classmethod
    def from_int(cls, value: int) -> "Num":
        if value < 0:
            raise ValueError("只支持非负数：%s" % value)
        if value == 0:
            return cls.zero()
        return cls(str(value)[::-1])

    # ── 输出 ──
    def is_zero(self) -> bool:
        return not self.digits

    def to_decimal(self) -> str:
        """转回常规十进制字符串（高位在前）。"""
        if not self.digits:
            return "0"
        return self.digits[::-1]

    def __str__(self) -> str:
        return self.to_decimal()

    def __repr__(self) -> str:
        return "Num(%s)" % self.to_decimal()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Num):
            return NotImplemented
        return self.digits == other.digits

    # ── 运算 ──
    def add(self, other: "Num") -> "Num":
        """大数加法：逐位相加，进位传向高位。"""
        if not isinstance(other, Num):
            raise TypeError("add() 只接受 Num")
        if other.is_zero():
            return self
        if self.is_zero():
            return other

        left = self.digits
        right = other.digits
        out = []
        carry = 0
        length = max(len(left), len(right))
        for i in range(length):
            digit_left = ord(left[i]) - 48 if i < len(left) else 0
            digit_right = ord(right[i]) - 48 if i < len(right) else 0
            total = digit_left + digit_right + carry
            out.append(chr(48 + total % 10))
            carry = total // 10
        if carry:
            out.append(chr(48 + carry))
        return Num("".join(out))

    def minus_one(self) -> "Num":
        """减一（结果必须为正，0 减一报错）。"""
        if self.is_zero():
            raise ValueError("0 不能减一")
        out = list(self.digits)
        for i, char in enumerate(out):
            if char != "0":
                out[i] = chr(48 + (ord(char) - 48 - 1))
                break
            out[i] = "9"
        return Num("".join(out).rstrip("0"))


# ─────────────────────────────────────────────
# 斐波那契
# ─────────────────────────────────────────────

def fibonacci_native(limit: int) -> int:
    """第 limit 项，Python 内置 int（任意精度）。

    F(1)=1, F(2)=1, F(3)=2, F(4)=3, F(5)=5, ...
    """
    if limit <= 0:
        return 0
    first, second = 1, 1
    for _ in range(3, limit + 1):
        first, second = second, first + second
    return second


def fibonacci_manual(limit: int) -> Num:
    """第 limit 项，用手写的大数加法（原 Java 实现的算法）。"""
    if limit <= 0:
        return Num.zero()
    if limit <= 2:
        return Num.from_int(1)
    first = Num.from_int(1)
    second = Num.from_int(1)
    for _ in range(3, limit + 1):
        first, second = second, first.add(second)
    return second


def fibonacci_sum_native(n: int) -> int:
    """前 n 项之和：F(1)+...+F(n) = F(n+2) - 1。"""
    if n <= 0:
        return 0
    return fibonacci_native(n + 2) - 1


def fibonacci_sum_manual(n: int) -> Num:
    """前 n 项之和，用手写的大数运算。"""
    if n <= 0:
        return Num.zero()
    return fibonacci_manual(n + 2).minus_one()


def fibonacci(limit: int, engine: str = ENGINE_NATIVE) -> str:
    """第 limit 项的十进制字符串，按引擎选择实现。"""
    if engine == ENGINE_MANUAL:
        return fibonacci_manual(limit).to_decimal()
    return str(fibonacci_native(limit))


def fibonacci_sum(n: int, engine: str = ENGINE_NATIVE) -> str:
    """前 n 项之和的十进制字符串。"""
    if engine == ENGINE_MANUAL:
        return fibonacci_sum_manual(n).to_decimal()
    return str(fibonacci_sum_native(n))


# ─────────────────────────────────────────────
# 自检
# ─────────────────────────────────────────────

def _check(passed: bool, what: str) -> bool:
    print(("[OK]   " if passed else "[FAIL] ") + what)
    return passed


def self_test() -> bool:
    """自检：大数加法 + 斐波那契恒等式 + 两种引擎交叉验证。"""
    ok = True

    # 1. 已知值（F(100) 已经超出 64 位，用字符串比对）
    known = [
        (1, "1"), (2, "1"), (3, "2"), (10, "55"), (20, "6765"),
        (30, "832040"), (50, "12586269025"), (100, "354224848179261915075"),
    ]
    for index, expected in known:
        ok = _check(fibonacci_native(index) == int(expected),
                    "F(%d) = %s" % (index, expected)) and ok

    # 2. 递推恒等式：F(n) + F(n+1) = F(n+2)
    mismatch = [n for n in range(1, 201)
                if fibonacci_native(n) + fibonacci_native(n + 1)
                != fibonacci_native(n + 2)]
    ok = _check(not mismatch, "递推恒等式 F(n)+F(n+1)=F(n+2) （n=1..200）") and ok

    # 3. 求和恒等式：sum(F(1..n)) = F(n+2) - 1
    mismatch = [n for n in (1, 2, 3, 10, 50, 100, 500)
                if sum(fibonacci_native(i) for i in range(1, n + 1))
                != fibonacci_sum_native(n)]
    ok = _check(not mismatch, "求和恒等式 sum(F(1..n)) = F(n+2) - 1") and ok

    # 4. 两种引擎交叉验证（手写大数实现 vs Python 原生任意精度 int）
    mismatch = [n for n in range(1, 301)
                if fibonacci_manual(n).to_decimal() != str(fibonacci_native(n))]
    ok = _check(not mismatch, "引擎交叉验证：manual == native （n=1..300）") and ok

    mismatch = [n for n in (1, 2, 10, 100, 500)
                if fibonacci_sum_manual(n).to_decimal() != str(fibonacci_sum_native(n))]
    ok = _check(not mismatch, "引擎交叉验证：求和 manual == native") and ok

    # 5. 手写大数加法的边界用例
    ok = _check(Num.from_decimal("999").add(Num.from_decimal("1")).to_decimal() == "1000",
                "进位链 999 + 1 = 1000") and ok
    ok = _check(Num.from_decimal("000123").add(Num.zero()).to_decimal() == "123",
                "前导零 000123 + 0 = 123") and ok
    ok = _check(Num.from_decimal("1000").minus_one().to_decimal() == "999"
                and Num.from_decimal("10").minus_one().to_decimal() == "9",
                "减一 1000 - 1 = 999，10 - 1 = 9") and ok
    ok = _check(Num.from_decimal("9999").add(Num.zero()).to_decimal() == "9999"
                and Num.zero().add(Num.from_decimal("1")).to_decimal() == "1",
                "空串视作 0：9999 + 0 = 9999，0 + 1 = 1") and ok

    return ok


# ─────────────────────────────────────────────
# 命令行
# ─────────────────────────────────────────────

USAGE = """用法：python fibonacci.py [maxIndex] [--sum] [--selftest] [--out FILE]
                        [--engine native|manual]

  maxIndex        计算到第几项，默认 %d（F(1)=1, F(2)=1, ...）
  --sum           只输出前 maxIndex 项之和 sum(F(1..n)) = F(n+2) - 1
  --selftest      自检大数加法与斐波那契恒等式，不输出数列
  --out FILE      输出写到文件（UTF-8），而不是标准输出
  --engine        native=用 Python 原生任意精度 int（默认）
                  manual=用手写的字符串大数加法（原 Java 实现的算法）

示例：
  python fibonacci.py 20
  python fibonacci.py 1000 --sum
  python fibonacci.py 50 --engine manual
  python fibonacci.py --selftest
""" % DEFAULT_MAX_INDEX


def _parse_args(argv):
    max_index = DEFAULT_MAX_INDEX
    sum_only = False
    selftest_only = False
    out_file = None
    engine = ENGINE_NATIVE

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--sum":
            sum_only = True
        elif arg == "--selftest":
            selftest_only = True
        elif arg == "--out":
            index += 1
            if index >= len(argv):
                raise ValueError("--out 后面要跟文件路径")
            out_file = argv[index]
        elif arg == "--engine":
            index += 1
            if index >= len(argv):
                raise ValueError("--engine 后面要跟 native 或 manual")
            engine = argv[index]
            if engine not in (ENGINE_NATIVE, ENGINE_MANUAL):
                raise ValueError("未知引擎：%s（可选 native 或 manual）" % engine)
        elif arg in ("--help", "-h"):
            print(USAGE, end="")
            return None
        elif arg.startswith("-"):
            raise ValueError("未知参数：%s" % arg)
        else:
            try:
                max_index = int(arg)
            except ValueError:
                raise ValueError("maxIndex 必须是整数：%s" % arg)
            if max_index < 0:
                raise ValueError("maxIndex 不能为负数")
        index += 1

    return max_index, sum_only, selftest_only, out_file, engine


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)

    try:
        parsed = _parse_args(argv)
    except ValueError as error:
        print("错误：%s" % error, file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    if parsed is None:
        return 0

    max_index, sum_only, selftest_only, out_file, engine = parsed

    if selftest_only:
        if self_test():
            print("自检全部通过。")
            return 0
        print("自检失败。")
        return 1

    if sum_only:
        lines = ["sum(F(1..%d)) = %s" % (max_index, fibonacci_sum(max_index, engine))]
    else:
        lines = ["%d=%s" % (i, fibonacci(i, engine)) for i in range(1, max_index + 1)]

    # 用 "\n" 拼接，交给平台的换行转换处理——与 Java println 的行为一致
    payload = "\n".join(lines) + "\n"

    if out_file is None:
        sys.stdout.write(payload)
        return 0

    try:
        with open(out_file, "w", encoding="utf-8", newline=None) as handle:
            handle.write(payload)
    except OSError as error:
        print("错误：写入 %s 失败：%s" % (out_file, error), file=sys.stderr)
        return 1
    print("已写入 %s（%d 字符）" % (out_file, len(payload)))
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出中文在 VS Code / Windows Terminal 下会乱码，
    # 统一按 UTF-8 输出（与仓库里 run.py / mini_agent.py 的做法一致）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
        raise SystemExit(130)

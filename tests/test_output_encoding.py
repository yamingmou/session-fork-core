#!/usr/bin/env python3
# role: test — 非 UTF-8 终端下的输出安全（Windows 兼容性里可回归的那部分）
"""非 UTF-8 输出安全：stdout 不得因装饰字符抛 UnicodeEncodeError。

为什么（2026-09-18 实测）：中文 Windows 的默认代码页是 cp936(GBK)，而 stdout 的
errors 默认是 `strict` —— 输出含 emoji 时 print 直接抛异常，**命令被"提示"本身打断**：
实测 `--verify` 退出码 1，且报错行自身也含 ❌ 从而二次崩（连错误信息都打不出来）。
`harden_output()` 在**非交互输出**时把 stdout 固定为 UTF-8，使中文与 emoji 都能完整输出。

覆盖边界（报绿必须交代）：
  ✅ 覆盖：非 tty（管道/重定向）+ 非 UTF-8 locale 下，进程能否完成输出且**内容零损失**；
           同时覆盖中文 Windows(cp936) 与英文 Windows(cp1252) 两种 locale。
  ❌ 未覆盖：真实 Windows 控制台（WindowsConsoleIO / UTF-16 路径）的行为 —— 需真机；
             也未覆盖 SQLite 锁竞争、`os.replace` 的目标占用行为（同属 Windows 特有）。

用法：python3 tests/test_output_encoding.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# 必须选一个「容纳不了 emoji」的编码，否则测不出问题。
# cp936 = 中文 Windows 默认；它同时是 Python 内置 codec，三平台都有。
ENCODING = "cp936"

# 探针：🩺 ✅ ❌ + 中文正文（用转义写，源文件保持纯 ASCII）
BODY = (
    "print('\\U0001fa7a \\u2705 \\u274c \\u4e2d\\u6587\\u6b63\\u6587')\n"
    "print('PROBE-OK')\n"
)


def run(with_fix: bool, encoding: str = ENCODING):
    """在管道 + 指定（非 UTF-8）编码下跑输出探针。"""
    src = "import os, sys\n"
    if with_fix:
        src += (
            "sys.path.insert(0, os.environ['FORK_TEST_ROOT'])\n"
            "from fork_core.adapter_base import harden_output\n"
            "harden_output()\n"
        )
    src += BODY
    env = dict(os.environ, PYTHONIOENCODING=encoding, FORK_TEST_ROOT=ROOT)
    return subprocess.run([PY, "-c", src], env=env, capture_output=True)


# ① 正例：加固后必须跑完
fixed = run(True)
assert fixed.returncode == 0, (
    "加固后仍失败（退出码 %s）\nstderr: %s"
    % (fixed.returncode, fixed.stderr.decode("utf-8", "replace"))
)
assert b"PROBE-OK" in fixed.stdout, "输出被中途打断（未到达最后一行）"
# 内容必须**零损失**：UTF-8 能容纳中文与 emoji —— 这正是用它替代 errors="replace" 的原因
# （replace 在英文 Windows 的 cp1252 下会把全部中文变成 "?"：命令"成功"但信息全丢）
assert "\u4e2d\u6587\u6b63\u6587".encode("utf-8") in fixed.stdout, "中文丢失（UTF-8 下不该丢）"
assert "\U0001fa7a".encode("utf-8") in fixed.stdout, "emoji 丢失（UTF-8 下不该丢）"

# ①b 英文 Windows（cp1252）：它连 ✓ 都编不了，是「中文全变成 ?」的高危场景
win_en = run(True, "cp1252")
assert win_en.returncode == 0, "cp1252 下加固后仍失败"
assert "\u4e2d\u6587\u6b63\u6587".encode("utf-8") in win_en.stdout, "cp1252 下中文丢失"

# ② 负例：不加固时同一段代码**必须失败** —— 证明本测试确有检出能力
#    （若这条也通过，说明该环境本就能编码 emoji，本测试是空转的、报绿无意义）
broken = run(False)
assert broken.returncode != 0, (
    "未加固也通过了 ⇒ 本测试在 %s 下无检出能力（该编码竟能容纳 emoji？）——"
    " 请确认 ENCODING 选的是不支持 emoji 的编码" % ENCODING
)

# ③ 设计依据的可回归断言：stderr 无需加固（其默认 errors 是 backslashreplace）
probe = "import sys\nprint('\\U0001fa7a', file=sys.stderr)\nprint('ERR-OK')\n"
r3 = subprocess.run(
    [PY, "-c", probe],
    env=dict(os.environ, PYTHONIOENCODING=ENCODING),
    capture_output=True,
)
assert r3.returncode == 0, "stderr 竟然抛异常 —— 与「stderr 默认 backslashreplace」的认知不符"
assert b"ERR-OK" in r3.stdout

print("\u2705 输出编码安全：%s 下加固后跑通（装饰符号退化、中文完整），未加固可被检出" % ENCODING)
print("   stderr 无需加固（默认 backslashreplace）—— 只加固 stdout 是有依据的")
print("   未覆盖：真实 Windows 控制台的 ConsoleIO 路径（需真机）")

#!/usr/bin/env python3
"""从单一源生成两份 SKILL.md（按渠道给对应的命令入口形态）。

为什么需要它：
    同一份 SKILL.md 给两类读者时，**弱模型会照抄文档里"出现最多的那种命令写法"**——
    · 示例写裸 `fork` ⇒ 非 WorkBuddy 读者 6/6，WorkBuddy 读者只有 2/6；
    · 示例写 WorkBuddy 绝对路径 ⇒ WorkBuddy 读者 6/6，非 WorkBuddy 读者只剩 1/6。
    ⇒ 入口形态**必须按渠道在构建期定死**，不能让模型在文档里自己选。

用法：
    python3 scripts/build_skill.py              # 生成：仓库根 SKILL.md（WB 版）+ dist/SKILL.fork.md（fork 版）
    python3 scripts/build_skill.py --check      # 只校验：产物与源是否一致（不一致即 exit 1，供发布前必检）
    python3 scripts/build_skill.py --pack       # 生成 + 打包 dist/clawhub/session-fork/（skills 目录渠道可直接上架）
                                                #   打包后会真跑一遍：入口可独立运行、6 个 adapter 都在

源文件 skill/SKILL.src.md 的标记约定（渲染时会被剥掉，不会出现在产物里）：
    {{FORK}}                        命令入口占位，按渠道替换（WB=绝对路径 / fork=`fork`）
    {{ADAPTER}}                     **按渠道决定要不要 `--adapter`**：
                                      WB  → 空串（WorkBuddy 是默认 adapter，写了反而多余）
                                      fork→ ` --adapter <你的产品>`（**非 WorkBuddy 必须写**，
                                            否则 CLI 默认按 workbuddy 处理 ⇒ 命令对读者是错的）
                                    加它的原因（2026-09-15 事实层核验抓到）：`--adapter` 的
                                    default 就是 workbuddy，所以 `--list` / `--verify` 也吃它
                                    ⇒ fork 版**每一条**命令都得带，不只是打分支那几条。
    <!--WBS:-->…<!--:WBS-->         仅 WorkBuddy 版保留
    <!--FKS:-->…<!--:FKS-->         仅 fork 版保留（同位置成对书写，渲染时二选一）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "skill" / "SKILL.src.md"
OUT_WB = REPO / "SKILL.md"
OUT_FORK = REPO / "dist" / "SKILL.fork.md"
PACK_DIR = REPO / "dist" / "clawhub" / "session-fork"

LONG_ENTRY = "python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py"
FORK_ENTRY = "fork"

ADAPTER_WB = ""
ADAPTER_FORK = " --adapter <你的产品>"

WBS = re.compile(r"<!--WBS:-->(.*?)<!--:WBS-->", re.S)
FKS = re.compile(r"<!--FKS:-->(.*?)<!--:FKS-->", re.S)


def render(text: str, variant: str) -> str:
    """variant: 'wb' | 'fork'"""
    if variant == "wb":
        text = FKS.sub("", text)          # 丢掉 fork-only 行
        text = WBS.sub(lambda m: m.group(1), text)
        entry = LONG_ENTRY
        adapter = ADAPTER_WB
    else:
        text = WBS.sub("", text)
        text = FKS.sub(lambda m: m.group(1), text)
        entry = FORK_ENTRY
        adapter = ADAPTER_FORK
    text = text.replace("{{FORK}}", entry)
    text = text.replace("{{ADAPTER}}", adapter)
    # 去掉因删块留下的连续空行（最多留一个空行）
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def pack(fork_text: str) -> int:
    """把 fork 版打成可直接上架的技能包（skills 目录渠道 / ClawHub）。

    结构（平台约束：最多两级目录）：
        session-fork/SKILL.md
        session-fork/LICENSE
        session-fork/scripts/create_branch.py     ← 唯一入口，自定位到技能根
        session-fork/fork_core/*.py
    打完**立刻真跑一遍**验证：包能独立于仓库运行、6 个 adapter 都在。
    """
    import shutil
    import subprocess

    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    (PACK_DIR / "scripts").mkdir(parents=True)
    (PACK_DIR / "fork_core").mkdir(parents=True)

    (PACK_DIR / "SKILL.md").write_text(fork_text, encoding="utf-8")
    shutil.copy2(REPO / "scripts" / "create_branch.py", PACK_DIR / "scripts")
    for p in sorted((REPO / "fork_core").glob("*.py")):
        shutil.copy2(p, PACK_DIR / "fork_core")
    if (REPO / "LICENSE").exists():
        shutil.copy2(REPO / "LICENSE", PACK_DIR)

    files = sorted(p.relative_to(PACK_DIR).as_posix() for p in PACK_DIR.rglob("*") if p.is_file())
    print(f"✓ 已打包 {PACK_DIR}")
    for f in files:
        print(f"    {f}")

    deep = [f for f in files if len(f.split("/")) > 2]
    if deep:
        print(f"✗ 超过两级目录（平台约束）：{deep}")
        return 1

    entry = PACK_DIR / "scripts" / "create_branch.py"
    py = sys.executable
    print("\n  打包后真跑自检：")
    try:
        r = subprocess.run([py, str(entry), "--version"], capture_output=True, text=True, cwd="/tmp")
        ok_ver = r.returncode == 0
        print(f"    --version      → {r.stdout.strip() or r.stderr.strip()}")
        if not ok_ver:
            print("✗ 打包后的入口跑不起来")
            return 1
        for a in ("workbuddy", "claude-code", "pi", "openclaw", "codex", "hermes"):
            rr = subprocess.run([py, str(entry), "--adapter", a, "--help"],
                                capture_output=True, text=True, cwd="/tmp")
            if rr.returncode != 0:
                print(f"✗ --adapter {a} 在包里不可用")
                return 1
        print("    6 个 adapter   → 全部可用")
        n_missing = fork_text.count("--adapter <你的产品>")
        print(f"    文档内 --adapter 覆盖 → {n_missing} 处")
    except Exception as e:  # noqa: BLE001
        print(f"✗ 自检异常：{e}")
        return 1
    return 0


def main() -> int:
    check = "--check" in sys.argv
    text = SRC.read_text(encoding="utf-8")

    if "{{FORK}}" not in text:
        print("✗ 源文件里没有 {{FORK}} 占位——构建产物被当成了源？")
        return 1
    if "{{ADAPTER}}" not in text:
        print("✗ 源文件里没有 {{ADAPTER}} 占位——fork 版会缺 `--adapter`（对读者是错命令）")
        return 1
    if re.search(r"<!--(WBS|FKS|:WBS|:FKS):?-->", text.replace("<!--WBS:-->", "").replace("<!--:WBS-->", "")
                 .replace("<!--FKS:-->", "").replace("<!--:FKS-->", "")):
        print("✗ 源文件里存在未闭合的标记")
        return 1

    wb = render(text, "wb")
    fork = render(text, "fork")

    leftover = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", wb + fork)))
    if leftover:
        print(f"✗ 渲染后仍有未替换的占位符：{leftover}")
        return 1

    if check:
        bad = []
        for path, want, label in ((OUT_WB, wb, "SKILL.md"), (OUT_FORK, fork, "dist/SKILL.fork.md")):
            if not path.exists():
                bad.append(f"{label} 不存在")
            elif path.read_text(encoding="utf-8") != want:
                bad.append(f"{label} 与源不一致（需重新生成）")
        if bad:
            for b in bad:
                print(f"✗ {b}")
            return 1
        print("✓ 两份产物都与源一致")
        return 0

    OUT_WB.write_text(wb, encoding="utf-8")
    OUT_FORK.parent.mkdir(parents=True, exist_ok=True)
    OUT_FORK.write_text(fork, encoding="utf-8")

    def stat(label: str, s: str) -> None:
        wb_marks = len(re.findall(r"WorkBuddy|workbuddy", s))
        fk_marks = len(re.findall(r"(?<![A-Za-z])fork ", s))
        print(f"  {label:22s} {len(s):6d} 字符 | WorkBuddy 提及 {wb_marks:3d} | `fork ` 形态 {fk_marks:3d}")

    print("✓ 已生成：")
    stat("SKILL.md（WB 版）", wb)
    stat("dist/SKILL.fork.md", fork)

    if "--pack" in sys.argv:
        print()
        return pack(fork)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    python3 scripts/build_skill.py --pack       # 生成 + 打两个渠道包：
                                                #   dist/wb/session-fork/       ← WorkBuddy 开放平台 / SkillHub
                                                #   dist/clawhub/session-fork/  ← ClawHub / 任意产品 skills 目录
                                                #   打包后逐个真跑：入口能脱离仓库运行、6 个 adapter 都在

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

# 非 UTF-8 终端（Windows 中文=cp936 / 英文=cp1252）下，下面的 print 含 ✓ 与中文，
# 会直接抛 UnicodeEncodeError 而**把构建打断**（CI 实测）。与 fork_core.harden_output
# 同一逻辑，此处内联以让构建脚本保持零依赖。
try:
    if not (sys.stdout.encoding or "").lower().startswith("utf"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 —— 非 TextIOWrapper（如 BytesIO）等：无害跳过
    pass

# ⚠️ 这是 SKILL.md 里**所有命令示例**的入口形态来源（占位符 {{FORK}} 的替换值）。
# 取技能目录的片段已把 SK 的尾斜杠去掉（`SK="${SK%/}"`），故此处必须写 ${SK}/scripts/…
LONG_ENTRY = 'python3 "${SK}/scripts/create_branch.py"'
FORK_ENTRY = "fork"

ADAPTER_WB = ""
ADAPTER_FORK = " --adapter <你的产品>"

WBS = re.compile(r"<!--WBS:-->(.*?)<!--:WBS-->", re.S)
FKS = re.compile(r"<!--FKS:-->(.*?)<!--:FKS-->", re.S)


def marker_errors(text: str) -> list:
    """检查渠道标记**成对、有序、不交叉**——不能只查"有没有写错的标记"。

    为什么必须有这一条（2026-09-19 定位）：`--check` 只比对「产物 vs 源」，
    **源里标记错配时两边一起错，对不出来**。曾经就有一对错配潜伏了很久：
    `<!--:FKS-->` 孤儿闭标记 + `<!--FKS:-->` 孤儿开标记 ⇒ fork-only 的两行留在了 wb 包，
    于是 SkillHub / 开放平台那份 SKILL.md 里同时出现「统一写成 WorkBuddy 形式」和
    「统一写成 `fork …`（你已用 pip 装了本工具）」两段自相矛盾的入口说明，外加一条
    与本渠道无关的 pip 安装说明。
    """
    errors = []
    pos = {}
    for name in ("WBS", "FKS"):
        opens = [m.start() for m in re.finditer(re.escape("<!--" + name + ":-->"), text)]
        closes = [m.start() for m in re.finditer(re.escape("<!--:" + name + "-->"), text)]
        pos[name] = (opens, closes)
        if len(opens) != len(closes):
            errors.append("%s 标记不配对：开 %d / 闭 %d" % (name, len(opens), len(closes)))
            continue
        for i, (o, c) in enumerate(zip(opens, closes), 1):
            if c < o:
                errors.append(
                    "%s 第 %d 个闭标记出现在第 %d 个开标记之前"
                    "（孤儿闭标记 ⇒ 这段内容两个渠道都会保留）" % (name, i, i)
                )
                break
    # 交叉嵌套：一个渠道的标记不能落在另一个渠道的区间里面
    for a, b in (("WBS", "FKS"), ("FKS", "WBS")):
        oa, ca = pos.get(a, ([], []))
        ob, cb = pos.get(b, ([], []))
        for s, e in zip(oa, ca):
            for p in ob + cb:
                if s < p < e:
                    errors.append("%s 区间内嵌了 %s 标记（交叉嵌套，渲染结果不可预期）" % (a, b))
                    break
            else:
                continue
            break
    return errors


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


def _english_description(text: str) -> str:
    """ClawHub 版：把 frontmatter 的 `description` 换成英文。

    为什么按渠道分（**不同平台是不同的读者**）：
      · SkillHub / WorkBuddy 开放平台 → 受众是中文用户，`description` 用中文；
      · **ClawHub → 受众是 OpenClaw / agent 生态，以英文为主** ⇒ 简介用英文。
    中文简介不丢：它仍保留在 frontmatter 的 `description_zh` 里，正文也仍是中文。
    """
    m = re.search(r"^description_en:\s*(.+)$", text, re.M)
    if not m:
        return text
    return re.sub(r"^description:.*$", "description: " + m.group(1).strip(),
                  text, count=1, flags=re.M)


def _pack_one(label: str, dirname: str, skill_text: str) -> int:
    """打一个渠道包：dist/<dirname>/session-fork/{SKILL.md,scripts/create_branch.py,fork_core/*.py}"""
    import shutil

    out = REPO / "dist" / dirname / "session-fork"
    if out.exists():
        shutil.rmtree(out)
    (out / "scripts").mkdir(parents=True)
    (out / "fork_core").mkdir(parents=True)
    with open(out / "SKILL.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(skill_text)
    shutil.copy2(REPO / "scripts" / "create_branch.py", out / "scripts")
    for p in sorted((REPO / "fork_core").glob("*.py")):
        shutil.copy2(p, out / "fork_core")
    # ⚠️ 不放 LICENSE：SkillHub 的 publish 会以「不允许的文件类型: LICENSE」拒收（无扩展名）。
    #    许可在 SKILL.md frontmatter 的 `license: MIT` 里已声明，包内不需要副本。
    #    （曾于 2026-09-15 首次发布 v2.4.8 时踩到：整包 400 被拒，摘掉 LICENSE 后才 accepted。）

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    print(f"✓ {label} → dist/{dirname}/session-fork/（{len(files)} 个文件）")
    deep = [f for f in files if len(f.split("/")) > 2]
    if deep:
        print(f"  ✗ 超过两级目录（平台约束）：{deep}")
        return 1
    return 0


def pack(wb_text: str, fork_text: str) -> int:
    """把两份产物各打成一个可直接上架的技能包，并**逐个真跑自检**。

    · dist/wb/session-fork/       ← WorkBuddy 开放平台 / SkillHub（SKILL.md = 产品感知的 WB 版）
    · dist/clawhub/session-fork/  ← ClawHub / 任意产品的 skills 目录（SKILL.md = fork 版，命令自带 --adapter）

    结构（平台约束：最多两级目录）：SKILL.md · scripts/create_branch.py · fork_core/*.py（共 15 文件，不含 LICENSE——见 _pack_one 说明）
    create_branch.py 自定位到技能根，所以包能脱离仓库独立运行。
    """
    import subprocess

    if _pack_one("WorkBuddy / SkillHub", "wb", wb_text):
        return 1
    if _pack_one("ClawHub / skills 目录", "clawhub", _english_description(fork_text)):
        return 1

    py = sys.executable
    print("\n  打包后真跑自检（在无关 cwd 下执行，验证自定位）：")
    for label, dirname in (("WB 包", "wb"), ("fork 包", "clawhub")):
        entry = REPO / "dist" / dirname / "session-fork" / "scripts" / "create_branch.py"
        r = subprocess.run([py, str(entry), "--version"], capture_output=True, text=True, cwd="/tmp")
        ver = r.stdout.strip() or r.stderr.strip()
        if r.returncode != 0:
            print(f"    ✗ {label} 入口跑不起来：{ver}")
            return 1
        bad = []
        for a in ("workbuddy", "claude-code", "pi", "openclaw", "codex", "hermes"):
            rr = subprocess.run([py, str(entry), "--adapter", a, "--help"],
                                capture_output=True, text=True, cwd="/tmp")
            if rr.returncode != 0:
                bad.append(a)
        if bad:
            print(f"    ✗ {label} 有 adapter 不可用：{bad}")
            return 1
        print(f"    {label:7s} {ver:12s} 6 个 adapter 全部可用")

    n = fork_text.count("--adapter <你的产品>")
    print(f"\n  fork 包文档内 --adapter 覆盖 → {n} 处")
    if n == 0:
        print("  ✗ fork 包文档一条 --adapter 都没有——读者会操作错产品的库")
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
    m_errs = marker_errors(text)
    if m_errs:
        for e in m_errs:
            print(f"✗ 渠道标记：{e}")
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

    with open(OUT_WB, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(wb)
    OUT_FORK.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FORK, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(fork)

    def stat(label: str, s: str) -> None:
        wb_marks = len(re.findall(r"WorkBuddy|workbuddy", s))
        fk_marks = len(re.findall(r"(?<![A-Za-z])fork ", s))
        print(f"  {label:22s} {len(s):6d} 字符 | WorkBuddy 提及 {wb_marks:3d} | `fork ` 形态 {fk_marks:3d}")

    print("✓ 已生成：")
    stat("SKILL.md（WB 版）", wb)
    stat("dist/SKILL.fork.md", fork)

    if "--pack" in sys.argv:
        print()
        return pack(wb, fork)
    return 0


if __name__ == "__main__":
    sys.exit(main())

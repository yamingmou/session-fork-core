#!/usr/bin/env python3
"""session-fork 化石回归 —— 用**真实会话样本**钉住行为，防"悄悄退化"。

为什么需要它（与表达层探针的分工）：
  - 清晰度探针只验"文档说清楚了吗"；**它验不了"命令真跑得通、真产出对的结果"**。
  - 本脚本是**事实层**：拿真实 transcript 真跑 `--dry-run`（零写入），把关键事实钉成基线，
    改代码后逐条 diff —— 任何一条漂移即非零退出。

设计上最关键的一点：**基线不能包含"活会话"**。
  活跃会话每轮都在长（本仓库自己就是例子：一轮对话让行数 +几），把它的行数写进基线
  必然"每跑必红"。所以每条化石分两类断言：
    · `exact`      —— 精确值（仅用于**已关闭、内容固定**的样本：cut/total/替换数）
    · `invariant`  —— 不变量（对活跃样本也成立：verify 必须 OK、残留必须为 0、入口必须存在）

纪律：**全程只读**（一律 `--dry-run`，绝不写用户的真实会话库）。

用法：
  python3 tests/regress_fossils.py --baseline-out /tmp/fossils.json   # 采基线
  python3 tests/regress_fossils.py --baseline-in  /tmp/fossils.json   # 回归（漂移即非零退出）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

#: ⚠️ **化石 id 不进仓库**：真实会话 id 属用户数据，写进本文件会随公开仓库一起发出去。
#: 因此化石清单来自**本地配置**（默认 `tests/fossils.local.json`，已 gitignore；
#: 可用环境变量 `FOSSILS_CONFIG` 覆盖）。仓库里只保留这个脚本与格式约定。
FOSSILS_CONFIG = os.environ.get("FOSSILS_CONFIG") or os.path.join(
    REPO, "tests", "fossils.local.json")

#: 配置格式：{"fossils": [{"id": "...", "why": "...", "stable": true|false}, ...]}
#: `stable=True` ⇒ 该会话已关闭、内容不再变 ⇒ 可做精确断言（cut/total）。
#: **绝不要把活跃会话标成 stable**（它每轮都在长，写进基线必然"每跑必红"）。
FOSSILS: list[dict] = []


def load_fossils() -> list[dict]:
    global FOSSILS
    if not os.path.exists(FOSSILS_CONFIG):
        return []
    with open(FOSSILS_CONFIG, encoding="utf-8") as f:
        FOSSILS = json.load(f).get("fossils", [])
    return FOSSILS

RE_CUT = re.compile(r"Split\s*:\s*line\s+(\d+)\s*/\s*(\d+)")
RE_REPL = re.compile(r"in\s+(\d+)\s+structured fields")
RE_VERIFY = re.compile(r"Verify\s*:\s*(.+)")


def run_one(sid: str, adapter: str = "workbuddy", timeout: int = 600) -> dict:
    """对单个化石跑一次 dry-run，抽出可比对的事实。"""
    cmd = [PY, "-m", "fork_core.cli", "--adapter", adapter,
           "--session", sid, "--dry-run", "--name", "化石回归"]
    t = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    out = t.stdout + t.stderr
    rec = {"id": sid, "rc": t.returncode}
    m = RE_CUT.search(out)
    if m:
        rec["cut"], rec["total"] = int(m.group(1)), int(m.group(2))
    m = RE_REPL.search(out)
    if m:
        rec["replacements"] = int(m.group(1))
    m = RE_VERIFY.search(out)
    rec["verify_ok"] = bool(m and "OK" in m.group(1))
    rec["residue_zero"] = ("zero residue" in out) or rec.get("verify_ok", False)
    rec["entry_exists"] = os.path.exists(os.path.join(REPO, "scripts", "create_branch.py"))
    if not rec.get("verify_ok"):
        rec["tail"] = "\n".join(out.strip().splitlines()[-4:])[:400]
    return rec


def collect() -> list[dict]:
    rows = []
    for f in FOSSILS:
        try:
            r = run_one(f["id"])
        except subprocess.TimeoutExpired:
            r = {"id": f["id"], "rc": -1, "timeout": True}
        r["why"] = f["why"]
        r["stable"] = f["stable"]
        rows.append(r)
    return rows


def invariants(rec: dict) -> dict:
    """对所有化石都必须成立的量（活跃样本也适用）。"""
    return {k: rec.get(k) for k in ("rc", "verify_ok", "residue_zero", "entry_exists")}


def diff(baseline: list[dict], cur: list[dict]) -> list[str]:
    errs = []
    b = {r["id"]: r for r in baseline}
    for r in cur:
        old = b.get(r["id"])
        if old is None:
            errs.append(f"[新增] {r['id'][:8]} 不在基线里")
            continue
        for k, v in invariants(old).items():
            if r.get(k) != v:
                errs.append(f"[漂移·不变量] {r['id'][:8]} {k}: {v} → {r.get(k)}")
        if old.get("stable"):
            # ⚠️ 不能写成 `if k in old` —— 基线缺键就会被**静默跳过**，那是假通过。
            # 稳定样本必须在基线里同时具备 cut/total，缺任一即判"基线不完整"。
            for k in ("cut", "total"):
                if k not in old:
                    errs.append(f"[基线不完整] {r['id'][:8]} 缺 {k}（稳定样本必须记精确值）")
                elif r.get(k) != old.get(k):
                    errs.append(f"[漂移·精确值] {r['id'][:8]} {k}: {old.get(k)} → {r.get(k)}")
    for i in b:
        if i not in {r["id"] for r in cur}:
            errs.append(f"[缺失] {i[:8]} 基线里有、本次没跑")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-out", metavar="PATH")
    ap.add_argument("--baseline-in", metavar="PATH")
    a = ap.parse_args()

    if not load_fossils():
        print(f"（跳过）未找到本地化石配置：{FOSSILS_CONFIG}")
        print("        把 tests/fossils.local.json.example 复制成 tests/fossils.local.json、")
        print("        填入你本机**已关闭**的会话 id 即可启用（该文件已在 .gitignore 里，不会进仓库）。")
        return 0

    cur = collect()
    print(f"{'样本':<10}{'行(cut/total)':<20}{'替换':<8}{'verify':<8}{'稳定':<6}说明")
    print("-" * 100)
    for r in cur:
        ct = f"{r.get('cut','?')}/{r.get('total','?')}"
        print(f"{r['id'][:8]:<10}{ct:<20}{str(r.get('replacements','-')):<8}"
              f"{'OK' if r.get('verify_ok') else 'FAIL':<8}"
              f"{'是' if r['stable'] else '否':<6}{r['why'][:40]}")

    if a.baseline_out:
        with open(a.baseline_out, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 基线已写入 {a.baseline_out}（{len(cur)} 条）")

    if a.baseline_in:
        base = json.load(open(a.baseline_in, encoding="utf-8"))
        errs = diff(base, cur)
        if errs:
            print(f"\n❌ 化石回归失败（{len(errs)} 处漂移）：")
            for e in errs:
                print("   " + e)
            return 1
        print(f"\n✅ 化石回归通过：{len(cur)} 条全部与基线一致（不变量 + 稳定样本的精确值）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

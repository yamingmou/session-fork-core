#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由字段普查（**只读**诊断工具）：回答"除了 sessionId，还有哪些字段在承载会话 id"。

为什么需要它
------------
`fork --verify` 里有一层"结构性残留"硬拦：产物里凡是**会话关联字段**装着源会话 id，
就判污染（因为产品会按它把消息路由回源会话）。这层检查认哪些键名，必须是**取证过**的，
不能凭字段名像不像就加——加错了会命中 `text` / `content` / `arguments` 这类**内容**字段，
把"在分支里写血缘"这种正常内容重新变成常驻红（v2.4.15 修的就是这个）。

用法
----
    # ① 键名普查：一个目录树下的 JSONL 里，哪些键的值是 uuid 形态？
    python3 tools/routing-key-survey.py keys --projects ~/.workbuddy/projects

    # ② 分支产物分析：真实分支里"值含父会话 id"的键分布（= 加进硬拦会新增多少条红）
    python3 tools/routing-key-survey.py branches --home ~

    # ③ 候选探测：手上有一批候选键名，想知道它们在数据里到底出现多少次
    python3 tools/routing-key-survey.py keys --projects <dir> \
        --candidate thread_id --candidate conversationId --candidate sessionId

**覆盖范围（务必看清再引用它的结论）**：本工具按"JSONL 目录树"扫描，因此它天然适配
**文件型后端**（WorkBuddy / Claude Code / pi / Codex）。SQLite 型后端（Hermes、OpenClaw 的
`openclaw-agent.sqlite`）**不在扫描范围内**——那些后端请对照各自 `adapter_*.py` 的存储契约
（文档字符串里通常写明了路由列/键名），必要时用 sqlite3 单独查。

判据是"**键名 + 值含 uuid**"：所以**非 uuid 形态的会话标识**（例如 OpenClaw 的
`session_key` = `agent:<id>:<key>`）不会被它发现——这也是为什么用它得出的"清单已足够"
只适用于扫描到的数据，不能外推到没扫的后端。
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
LINEAGE_NAME = "fork.lineage.json"


def parse(path):
    """逐行容错解析（坏行跳过）——与产品读取口径一致。"""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        pass
    return out


def walk(node, path=""):
    """产出 (路径, 键名, 值)——只看 dict 的键，与结构性检查的遍历口径一致。"""
    if isinstance(node, dict):
        for k, v in node.items():
            full = f"{path}.{k}" if path else k
            yield full, k, v
            yield from walk(v, full)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, path)


def _jsonl_files(root):
    for dirpath, _dirs, names in os.walk(root):
        for fn in names:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def cmd_keys(args):
    cands = set(args.candidate or [])
    uuid_keys, cand_hits = Counter(), Counter()
    files = 0
    for p in _jsonl_files(args.projects):
        files += 1
        for o in parse(p):
            for _path, key, val in walk(o):
                if isinstance(val, str) and UUID_RE.search(val):
                    uuid_keys[key] += 1
                if cands and key in cands:
                    cand_hits[key] += 1
    print(f"═══ 键名普查：{files} 个 JSONL（{args.projects}）═══")
    print("  值含 uuid 的键（按出现次数）:")
    for key, n in uuid_keys.most_common(30):
        print(f"    {n:>8}  {key}")
    if cands:
        print("\n  候选键的**总出现次数**（不看值形态）:")
        for key in sorted(cands):
            print(f"    {cand_hits[key]:>8}  {key}")
        print("  说明：总出现次数 > 0 只代表「这个键存在」；要判断它是否承载会话 id，")
        print("        得看它是否同时出现在上面的 uuid 键清单里（或人工核对存储契约）。")


def cmd_branches(args):
    lineage = os.path.join(args.home, ".workbuddy", LINEAGE_NAME)
    if not os.path.isfile(lineage):
        sys.exit(f"找不到谱系：{lineage}")
    forks = [f for f in json.load(open(lineage, encoding="utf-8")).get("forks", [])
             if f.get("parent_id")]
    projects = os.path.join(args.home, ".workbuddy", "projects")
    hits, per_branch, checked = Counter(), Counter(), 0
    for f in forks:
        bid, pid = f["id"], f["parent_id"]
        path = next((p for p in _jsonl_files(projects)
                     if os.path.basename(p).startswith(bid)), None)
        if not path:
            continue
        checked += 1
        seen = set()
        for o in parse(path):
            for _p, key, val in walk(o):
                if isinstance(val, str) and pid in val:
                    hits[key] += 1
                    seen.add(key)
        for k in seen:
            per_branch[k] += 1
    print(f"═══ 分支产物分析：{len(forks)} 条分支记录，找到文件 {checked} 条 ═══")
    print("  「值含父会话 id」的键 —— 右侧是**若把该键加入硬拦，会新增多少条红**:")
    for key, n in hits.most_common(25):
        print(f"    命中 {n:>6} 次 | 涉及 {per_branch[key]:>3} 条分支 | {key}")
    if not hits:
        print("    （无命中）")


def main():
    ap = argparse.ArgumentParser(description="路由字段普查（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keys", help="键名普查：哪些键的值是 uuid 形态")
    k.add_argument("--projects", required=True)
    k.add_argument("--candidate", action="append",
                   help="额外统计这些键名的总出现次数（可重复）")
    k.set_defaults(func=cmd_keys)

    b = sub.add_parser("branches", help="真实分支里「值含父会话 id」的键分布")
    b.add_argument("--home", default=os.path.expanduser("~"))
    b.set_defaults(func=cmd_branches)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

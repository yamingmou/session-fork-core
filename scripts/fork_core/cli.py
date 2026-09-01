"""fork_core.cli — 命令行入口（pip 安装后 `fork` / `fork-branch` 命令）。

与 scripts/create_branch.py 共用同一套逻辑；后者保留为 WorkBuddy 技能目录
内的兼容入口（调本模块 main）。
"""

import argparse
import os
import shutil
import sys
import time

from . import available, create_fork, get_adapter, list_forks

VERSION = "2.3.0"


def print_tree(metas) -> None:
    """按 parent_id 链打印谱系树（父会话 → 子分支 → 孙分支）。"""
    if not metas:
        print("🌳 暂无谱系（尚未创建分支）")
        return
    by_id = {m.id: m for m in metas}
    children: dict[str, list] = {}
    roots = []
    for m in metas:
        p = m.parent_id or ""
        if p and p in by_id:
            children.setdefault(p, []).append(m)
        else:
            roots.append(m)
    seen = set()

    def render(m, prefix="", is_last=True):
        branch_char = "└── " if is_last else "├── "
        label = m.title or m.id[:12]
        extra = ""
        if m.extra.get("at_seq") is not None:
            extra = f"  (atSeq={m.extra['at_seq']})"
        print(f"{prefix}{branch_char}{label}  [{m.id[:8]}]{extra}")
        kids = children.get(m.id, [])
        next_prefix = prefix + ("    " if is_last else "│   ")
        for i, k in enumerate(kids):
            render(k, next_prefix, i == len(kids) - 1)

    print("🌳 Fork 谱系树")
    for i, r in enumerate(roots):
        render(r, "", i == len(roots) - 1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="fork",
        description="Cross-product session forking (Fork = Projection Derivative).",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--session", help="source session id, or 'current'")
    ap.add_argument("--match", help="split-point text within the final assistant reply")
    ap.add_argument("--line", type=int, help="exact 1-based split line (alternative to --match)")
    ap.add_argument("--request-id", help="requestId from product UI 'Copy Request ID' (most precise)")
    ap.add_argument("--name", default=None, help="branch name (default: auto from topic)")
    ap.add_argument("--dry-run", action="store_true", help="only locate & report, write nothing")
    ap.add_argument("--list", action="store_true", dest="list_branches", help="list branches in current workspace")
    ap.add_argument("--tree", action="store_true", dest="tree", help="show fork lineage as a tree (with --list)")
    ap.add_argument("--fix", metavar="SESSION_ID", help="re-truncate a branch (workbuddy only)")
    ap.add_argument("--verify", "--doctor", action="store_true", dest="verify", help="real-database health check (L2), alias --doctor")
    ap.add_argument("--adapter", default="workbuddy", choices=available(), help="product adapter")
    args = ap.parse_args(argv)

    adapter = get_adapter(args.adapter)

    if args.verify:
        run_verify(adapter)
        return

    if args.list_branches:
        cwd = os.environ.get("WORKBUDDY_CWD")
        if args.tree and hasattr(adapter, "lineage_tree"):
            print_tree(adapter.lineage_tree(cwd))
            return
        branches = list_forks(adapter, cwd)
        if not branches:
            print("📂 当前工作区暂无分支")
        else:
            print(f"📂 当前工作区的分支列表（共 {len(branches)} 个）")
            for b in branches:
                print(f"  - {b.id[:12]}… | {b.title} | {b.status} | {b.created_at}")
        return

    if args.fix:
        if adapter.name != "workbuddy":
            raise SystemExit("--fix is only supported for the workbuddy adapter")
        run_fix(args.fix)
        return

    if not args.session:
        ap.error("--session is required (or use --list)")

    r = create_fork(
        adapter=adapter,
        session_ref=args.session,
        match_text=args.match,
        line_no=args.line,
        request_id=args.request_id,
        name=args.name,
        dry_run=args.dry_run,
    )

    print(f"Source   : {r.src_id}  ({r.transcript_path})")
    print(f"Split    : line {r.cut} / {r.total}  ({r.how})")
    print(f"Branch   : {r.new_id}  name={r.name!r}")
    if args.dry_run:
        print("DRY RUN — nothing written. (Would back up source jsonl only)")
        return
    if r.backup_dir:
        print(f"Backup   : {r.backup_dir} (source jsonl only, no database copy)")
    if r.replacements:
        print(f"Note     : replaced {r.src_id}->{r.new_id} in {r.replacements} structured fields")
    print(f"Verify   : OK ({r.cut} lines, sessionId consistent, zero residue, tail complete)")
    print(f"NEW SESSION ID: {r.new_id}")
    print(f"custom_title: {r.name}  | status: terminated")
    print(f"⚠️  注意：分支文件未锁定只读。如需防止主进程追加消息，请手动执行：chmod 444 {r.dst_path}")
    print(f"ACTION   : ⚠️ 请重启对应产品以在会话列表中看到新分支")


def run_verify(adapter) -> None:
    """fork --verify / --doctor：真库体检（L2 级，发布/打分支前必跑）。"""
    from .engine import verify_environment

    items = verify_environment(adapter)
    print(f"🩺 fork --verify — {adapter.name} 真库体检（验证分级：L1 fixture / L2 真库 / L3 产品终验）")
    print()
    ok_all = True
    for it in items:
        mark = "✅" if it.ok else "❌"
        print(f"  {mark} [{it.level}] {it.name}: {it.detail}")
        ok_all = ok_all and it.ok
    print()
    if ok_all:
        print("✅ 全部通过 — 该环境达到 L2 真库级验证，可安全打分支/发布")
    else:
        print("❌ 存在失败项 — 请修复后再打分支/发布（开发规范 §二·五：L2 真库验证是必须项）")
        sys.exit(1)


def run_fix(fix_session_id: str) -> None:
    """修复被主进程追加了多余消息的分支（WorkBuddy 专用）。"""
    from .engine import DEFAULT_BACKUPS_DIR, _load_lines, locate_last_reply

    wb = get_adapter("workbuddy")
    fix_path, _ = wb.find_transcript(fix_session_id)
    if not fix_path:
        raise SystemExit(f"Transcript not found for {fix_session_id}")

    lines = _load_lines(fix_path)
    total = len(lines)
    cut, _ = locate_last_reply(wb, lines)
    print(f"Source   : {fix_session_id}")
    print(f"Current  : {total} lines")
    print(f"Target   : {cut} lines (locate_last_reply)")
    if cut >= total:
        print("Already correct — no fix needed.")
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(DEFAULT_BACKUPS_DIR, ts)
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(fix_path, os.path.join(backup_dir, os.path.basename(fix_path)))
    print(f"Backup   : {backup_dir}")

    wb.write_branch(fix_path, lines[:cut])
    check = _load_lines(fix_path)
    print(f"Verify   : {len(check)} lines (was {total}, removed {total - len(check)})")
    print(f"⚠️  分支文件未锁定只读。如需防止追加消息，请手动执行：chmod 444 {fix_path}")


if __name__ == "__main__":
    sys.exit(main())

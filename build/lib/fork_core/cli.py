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

VERSION = "2.0.0"


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
    ap.add_argument("--fix", metavar="SESSION_ID", help="re-truncate a branch (workbuddy only)")
    ap.add_argument("--adapter", default="workbuddy", choices=available(), help="product adapter")
    args = ap.parse_args(argv)

    adapter = get_adapter(args.adapter)

    if args.list_branches:
        cwd = os.environ.get("WORKBUDDY_CWD")
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

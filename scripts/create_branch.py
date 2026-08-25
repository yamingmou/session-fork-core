#!/usr/bin/env python3
"""Create a branch copy of a WorkBuddy session, truncated at a given point.

Part of the "session-fork" (会话分叉 / 打分支) skill.
Built by the OfferKuai (Offer快) Team — https://www.offerkuai.com/ | contact@offerkuai.com
MIT License.

Usage:
  create_branch.py --session current [--name "分支名"]
      # DEFAULT mode: truncate at the end of the LAST completed reply
      # (the assistant output_text that precedes the latest user message —
      #  i.e. "上一轮对话的输出结束"). No split-point text needed.
  create_branch.py --session <session-id> --match "<split-point text>" [--name "分支名"]
  create_branch.py --session <session-id> --line <N> [--name "分支名"]

Behavior:
  1. Locate the session transcript jsonl under ~/.workbuddy/projects/<workspace-slug>/
  2. Find the split point:
       default  -> locate_last_reply(): last completed assistant reply before
                   the latest user message (previous turn's output end)
       --match  -> last assistant message containing the text
       --line   -> exact 1-based line
  3. Back up the jsonl + workbuddy.db to ~/.workbuddy/backups/<timestamp>/
  4. Copy lines 1..cut to a new jsonl with a fresh sessionId
  5. Rewrite the OLD session id -> NEW session id in ALL nested string fields
     (output.text / providerData.toolResult / arguments / reasoning / rawContent)
  6. Insert a new row into the sessions table (copy source row; change id /
     custom_title / status=terminated / timestamps)
  7. Verify: parseability, sessionId consistency, zero old-id residue, tail integrity
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid

VERSION = "1.3.0"

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".workbuddy", "projects")
DB_PATH = os.path.join(HOME, ".workbuddy", "workbuddy.db")
BACKUPS_DIR = os.path.join(HOME, ".workbuddy", "backups")


def slug_from_cwd(cwd):
    if not cwd:
        return None
    return cwd.strip("/").replace("/", "-")


def find_transcript(session_id):
    """Locate the jsonl for a session id across all workspace slugs."""
    for slug in os.listdir(PROJECTS_DIR):
        cand = os.path.join(PROJECTS_DIR, slug, session_id + ".jsonl")
        if os.path.exists(cand):
            return cand, slug
    return None, None


def resolve_session(session_id, db):
    """Resolve 'current' to the most recent working session, else validate id."""
    if session_id != "current":
        return session_id
    cur = db.cursor()
    cur.execute(
        "SELECT id FROM sessions WHERE status='working' ORDER BY created_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("No 'working' session found to branch from.")
    return row[0]


def locate_last_reply(path):
    """DEFAULT mode: truncate at the end of the LAST completed reply.

    The split point = the last assistant message with a non-empty output_text
    that precedes the LATEST user message — i.e. the previous turn's output
    end. This is what "每个分支的截断点应该就是上一轮对话的输出结束" means:
    when the user says 打分支 (often with no other instruction), the branch
    ends exactly where the previous turn's reply ended.
    """
    lines = open(path).read().splitlines()
    n = len(lines)
    last_user = None
    for i, l in enumerate(lines, 1):
        try:
            o = json.loads(l)
        except Exception:
            continue
        if o.get("type") == "message" and o.get("role") == "user":
            last_user = i
    limit = last_user if last_user is not None else n + 1
    for i in range(limit - 1, 0, -1):
        try:
            o = json.loads(lines[i - 1])
        except Exception:
            continue
        if o.get("type") != "message" or o.get("role") != "assistant":
            continue
        for c in o.get("content", []) or []:
            if (
                isinstance(c, dict)
                and c.get("type") == "output_text"
                and c.get("text", "").strip()
            ):
                return i, n
    raise SystemExit(
        "No completed assistant reply found before the latest user message"
    )


def locate_split_line(path, match_text=None, line_no=None, request_id=None):
    """Return the 1-based line number of the split point.

    With match_text: the LAST assistant message whose output_text contains it.
    With line_no: validate the line is an assistant message with output_text.
    With request_id: the LAST assistant message whose providerData.conversationRequestId matches.
                     (from WorkBuddy UI "Copy Request ID" button)
    Raises if the candidate line is not the tail boundary (next message must be
    a new user message or EOF) — a split point must be a complete reply.
    """
    lines = open(path).read().splitlines()
    n = len(lines)
    if request_id is not None:
        cand = None
        for i, l in enumerate(lines, 1):
            try:
                o = json.loads(l)
            except Exception:
                continue
            if o.get("type") != "message" or o.get("role") != "assistant":
                continue
            if (o.get("providerData") or {}).get("conversationRequestId") == request_id:
                cand = i  # keep last match
        if cand is None:
            raise SystemExit(f"request_id not found in any assistant reply: {request_id!r}")
    elif line_no is not None:
        cand = line_no
        if not (1 <= cand <= n):
            raise SystemExit(f"--line {cand} out of range (file has {n} lines)")
    else:
        cand = None
        for i, l in enumerate(lines, 1):
            try:
                o = json.loads(l)
            except Exception:
                continue
            if o.get("type") != "message" or o.get("role") != "assistant":
                continue
            for c in o.get("content", []) or []:
                if (
                    isinstance(c, dict)
                    and c.get("type") == "output_text"
                    and match_text in c.get("text", "")
                ):
                    cand = i  # keep last match
        if cand is None:
            raise SystemExit(f"match text not found in any assistant reply: {match_text!r}")
    # Tail-boundary check: line after split must be a user message or EOF
    o = json.loads(lines[cand - 1])
    if o.get("type") != "message" or o.get("role") != "assistant":
        raise SystemExit(f"Split line {cand} is not an assistant message")
    if cand < n:
        nxt = json.loads(lines[cand])
        if nxt.get("type") == "message" and nxt.get("role") != "user":
            raise SystemExit(
                f"Line {cand} is not a complete reply boundary (line {cand+1} is "
                f"{nxt.get('type')}/{nxt.get('role')})"
            )
    return cand, n


def list_branches(db, cwd=None):
    """List all branch sessions in the current workspace.

    Branches are identified by custom_title containing '·分支' or '分支·',
    or by any session whose cwd matches the given workspace.
    """
    cur = db.cursor()
    if cwd:
        cur.execute(
            "SELECT id, custom_title, status, created_at, cwd FROM sessions "
            "WHERE cwd=? ORDER BY created_at DESC",
            (cwd,),
        )
    else:
        cur.execute(
            "SELECT id, custom_title, status, created_at, cwd FROM sessions "
            "ORDER BY created_at DESC"
        )
    rows = cur.fetchall()
    branches = []
    for r in rows:
        title = r[1] or ""
        # Include if title looks like a branch or if explicitly requested
        if "分支" in title or "·" in title or "fork" in title.lower():
            ts = r[3]
            if isinstance(ts, int) and ts > 1e12:
                ts = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
            elif isinstance(ts, int):
                ts = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            branches.append((r[0], title, r[2], ts, r[4]))
    return branches


def extract_topic_hint(lines, last_user_line=None):
    """Extract a short topic hint from the last user message for default branch name."""
    if last_user_line is None:
        # Find last user message
        for i in range(len(lines) - 1, -1, -1):
            try:
                o = json.loads(lines[i])
                if o.get("type") == "message" and o.get("role") == "user":
                    last_user_line = i
                    break
            except Exception:
                continue
    if last_user_line is None:
        return ""
    try:
        o = json.loads(lines[last_user_line])
        for c in o.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "input_text":
                text = c.get("text", "").strip()
                if text:
                    # Skip system-like content
                    if text.startswith("<") or text.startswith("<!--"):
                        continue
                    # Take first line, skip if too short or looks like code/command
                    first_line = text.split("\n")[0].strip()
                    if len(first_line) < 2 or first_line.startswith(("!", "/", "#", "```")):
                        continue
                    if len(first_line) > 15:
                        first_line = first_line[:15] + "…"
                    return first_line
    except Exception:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--session", help="source session id, or 'current'")
    ap.add_argument("--match", help="split-point text within the final assistant reply")
    ap.add_argument("--line", type=int, help="exact 1-based split line (alternative to --match)")
    ap.add_argument("--request-id", help="conversationRequestId from WorkBuddy UI 'Copy Request ID' (most precise)")
    ap.add_argument("--name", default=None, help="custom_title suffix for the branch (default: auto from topic)")
    ap.add_argument("--dry-run", action="store_true", help="only locate & report, write nothing")
    ap.add_argument("--list", action="store_true", dest="list_branches", help="list all branches in current workspace")
    ap.add_argument("--fix", metavar="SESSION_ID", help="re-truncate a branch to its correct cut point")
    args = ap.parse_args()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # --list mode
    if args.list_branches:
        cwd = os.environ.get("WORKBUDDY_CWD")
        branches = list_branches(db, cwd)
        if not branches:
            print("📂 当前工作区暂无分支")
        else:
            print(f"📂 当前工作区的分支列表（共 {len(branches)} 个）")
            for bid, title, status, ts, bcwd in branches:
                print(f"  - {bid[:12]}… | {title} | {status} | {ts}")
        db.close()
        return

    if not args.session and not args.fix:
        ap.error("--session is required (or use --list or --fix)")

    # --fix mode: re-truncate a branch to its correct cut point
    if args.fix:
        fix_session_id = args.fix
        fix_path, fix_slug = find_transcript(fix_session_id)
        if not fix_path:
            raise SystemExit(f"Transcript not found for {fix_session_id}")

        # Read the DB to get the source session from the parentSession or cwd
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        cur = db.cursor()

        # The branch file may have extra lines appended by WorkBuddy after creation.
        # Strategy: find the first user/message that appears AFTER the cut point.
        # The cut point = locate_last_reply applied to the current (overgrown) file,
        # but we need the ORIGINAL cut point. Since the branch's last legitimate
        # assistant reply should be the one right before the first "post-creation" user
        # message, we detect this by looking for messages that reference the branch's
        # own session ID in a way that indicates they were created after the branch.

        # Actually simpler: the branch should end at the last assistant message
        # that has a non-empty output_text and is followed by a user message that
        # was NOT part of the original cut. We detect by checking if there are
        # messages after what locate_last_reply would find.

        cut, total = locate_last_reply(fix_path)
        lines = open(fix_path).read().splitlines()
        print(f"Source   : {fix_session_id}")
        print(f"Current  : {total} lines")
        print(f"Target   : {cut} lines (locate_last_reply)")
        if cut >= total:
            print("Already correct — no fix needed.")
            db.close()
            return

        # Backup
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(BACKUPS_DIR, ts)
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(fix_path, os.path.join(backup_dir, os.path.basename(fix_path)))
        print(f"Backup   : {backup_dir}")

        # Re-truncate
        truncated = lines[:cut]
        with open(fix_path, "w") as f:
            f.write("\n".join(truncated) + "\n")
        # Re-lock
        os.chmod(fix_path, 0o444)

        # Re-verify
        check = open(fix_path).read().splitlines()
        print(f"Verify   : {len(check)} lines (was {total}, removed {total - len(check)})")
        print(f"Locked   : {os.path.basename(fix_path)} set to read-only (0444)")
        db.close()
        return

    # default mode: no --match / --line -> truncate at previous turn's output end

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    src_id = resolve_session(args.session, db)

    cur = db.cursor()
    cur.execute("SELECT * FROM sessions WHERE id=?", (src_id,))
    src_row = cur.fetchone()
    if not src_row:
        raise SystemExit(f"Session not found in DB: {src_id}")
    cols = src_row.keys()

    transcript, slug = find_transcript(src_id)
    if not transcript:
        raise SystemExit(f"Transcript not found for {src_id} in {PROJECTS_DIR}")

    if args.match or args.line or args.request_id:
        cut, total = locate_split_line(transcript, args.match, args.line, args.request_id)
        how = f"match={args.match!r}" if args.match else (f"line={args.line}" if args.line else f"request_id={args.request_id!r}")
    else:
        cut, total = locate_last_reply(transcript)
        how = "default (previous turn's output end)"
    new_id = str(uuid.uuid4())

    # Auto-generate branch name from topic hint if not provided
    branch_name = args.name
    if branch_name is None:
        all_lines = open(transcript).read().splitlines()
        hint = extract_topic_hint(all_lines)
        branch_name = f"分支·{hint}" if hint else "分支"

    print(f"Source   : {src_id}  ({transcript})")
    print(f"Split    : line {cut} / {total}  ({how})")
    print(f"Branch   : {new_id}  name={branch_name!r}")
    ts = time.strftime("%Y%m%d-%H%M%S")
    if args.dry_run:
        print(f"DRY RUN — nothing written. (Would back up to {os.path.join(BACKUPS_DIR, ts)})")
        return

    backup_dir = os.path.join(BACKUPS_DIR, ts)
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(transcript, os.path.join(backup_dir, os.path.basename(transcript)))
    shutil.copy2(DB_PATH, os.path.join(backup_dir, "workbuddy.db"))
    print(f"Backup   : {backup_dir}")

    lines = open(transcript).read().splitlines()[:cut]
    out_lines = []
    for l in lines:
        o = json.loads(l)
        o["sessionId"] = new_id
        out_lines.append(json.dumps(o, ensure_ascii=False))
    dst = os.path.join(os.path.dirname(transcript), new_id + ".jsonl")
    with open(dst, "w") as f:
        f.write("\n".join(out_lines) + "\n")

    # Rewrite nested old-id residue (strings anywhere in the JSON tree)
    raw = open(dst).read()
    if src_id in raw:
        raw = raw.replace(src_id, new_id)
        with open(dst, "w") as f:
            f.write(raw)
        print(f"Note     : replaced {src_id}->{new_id} inside nested fields")

    # Lock the file to prevent WorkBuddy from appending more messages
    os.chmod(dst, 0o444)
    print(f"Locked   : {os.path.basename(dst)} set to read-only (0444)")

    # Insert DB row
    now_ms = int(time.time() * 1000)
    vals = {c: src_row[c] for c in cols}
    vals["id"] = new_id
    vals["custom_title"] = branch_name
    vals["status"] = "terminated"
    vals["created_at"] = now_ms
    vals["updated_at"] = now_ms
    vals["last_activity_at"] = now_ms
    ph = ",".join(["?"] * len(cols))
    cur.execute(
        f"INSERT INTO sessions ({','.join(cols)}) VALUES ({ph})",
        [vals[c] for c in cols],
    )
    db.commit()

    # Verify
    check = open(dst).read().splitlines()
    errs = []
    if len(check) != cut:
        errs.append(f"line count {len(check)} != {cut}")
    for i, l in enumerate(check, 1):
        try:
            o = json.loads(l)
        except Exception as e:
            errs.append(f"parse error line {i}: {e}")
            continue
        if o.get("sessionId") != new_id:
            errs.append(f"line {i} sessionId mismatch: {o.get('sessionId')}")
    if src_id in open(dst).read():
        errs.append("old session id still present")
    last = json.loads(check[-1])
    tail_ok = False
    for c in last.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
            tail_ok = True
    if not tail_ok:
        errs.append("last line has no assistant output_text")

    if errs:
        print("VERIFY FAILED:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print(f"Verify   : OK ({cut} lines, sessionId consistent, zero residue, tail complete)")
    print(f"NEW SESSION ID: {new_id}")
    print(f"custom_title: {branch_name}  | status: terminated")
    print(f"ACTION   : ⚠️ 请重启 WorkBuddy 以在会话列表中看到新分支")


if __name__ == "__main__":
    main()

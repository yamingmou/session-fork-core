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

VERSION = "1.0.0"

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


def locate_split_line(path, match_text=None, line_no=None):
    """Return the 1-based line number of the split point.

    With match_text: the LAST assistant message whose output_text contains it.
    With line_no: validate the line is an assistant message with output_text.
    Raises if the candidate line is not the tail boundary (next message must be
    a new user message or EOF) — a split point must be a complete reply.
    """
    lines = open(path).read().splitlines()
    n = len(lines)
    if line_no is not None:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--session", required=True, help="source session id, or 'current'")
    ap.add_argument("--match", help="split-point text within the final assistant reply")
    ap.add_argument("--line", type=int, help="exact 1-based split line (alternative to --match)")
    ap.add_argument("--name", default="分支", help="custom_title suffix for the branch")
    ap.add_argument("--dry-run", action="store_true", help="only locate & report, write nothing")
    args = ap.parse_args()
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

    if args.match or args.line:
        cut, total = locate_split_line(transcript, args.match, args.line)
        how = f"match={args.match!r}" if args.match else f"line={args.line}"
    else:
        cut, total = locate_last_reply(transcript)
        how = "default (previous turn's output end)"
    new_id = str(uuid.uuid4())

    print(f"Source   : {src_id}  ({transcript})")
    print(f"Split    : line {cut} / {total}  ({how})")
    print(f"Branch   : {new_id}  name={args.name!r}")
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

    # Insert DB row
    now_ms = int(time.time() * 1000)
    vals = {c: src_row[c] for c in cols}
    vals["id"] = new_id
    vals["custom_title"] = args.name
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
    print(f"custom_title: {args.name}  | status: terminated")


if __name__ == "__main__":
    main()

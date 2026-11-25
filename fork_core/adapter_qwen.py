"""Qwen（QwenWorkCN 桌面端）适配器：`agents.db`。

实测基线：QwenWorkCN 桌面端，库在 `~/Library/Application Support/QwenWorkCN/data/agents.db`
（11 会话 / 356 条消息）。

## 结构

    chats(id, name, project_id NOT NULL, created_at, updated_at, archived_at, deleted_at,
          worktree_path, output_directory, chat_type, ext, …)
    sub_chats(id, chat_id NOT NULL, session_id, mode NOT NULL, messages NOT NULL DEFAULT '[]', …)
    messages(id, message_id NOT NULL, chat_id, sub_chat_id, sequence, role,
             parts NOT NULL, metadata NOT NULL, searchable_text, search_status, created_at, updated_at)

## 三件必须照做的事实（都是实测）

1. ⭐ **`searchable_text` 是搜索的承载点**：FTS 触发器的 SQL 是
   `INSERT INTO messages_fts(...) VALUES (NEW.searchable_text, ...)` —— 只认这一列。
   不填 ⇒ 消息在界面上有、**搜不到**。所以写入侧必须自己把它填成纯文本。
2. **时间戳是秒**（`1793805524`），不是毫秒。
3. **`chats.project_id` NOT NULL 且指向 `projects` 表** ⇒ 打分支沿用源会话的 `project_id`
   （自造 project 没有依据；opencode 那边就是在这里翻过车）。

## 行形状

一行 = 一条 `messages` 记录，`parts` 已解析成 list：

    {"id", "message_id", "chat_id", "sub_chat_id", "sequence", "role",
     "parts": [...], "metadata": {...}, "searchable_text", "created_at", "updated_at"}
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import string
import time
from typing import Optional

from .adapter_base import TranscriptionAdapter
from .models import SessionMeta

DB_REL = "Library/Application Support/QwenWorkCN/data/agents.db"
REF_SEP = "#"
_ALPHABET = string.ascii_lowercase + string.digits


def make_ref(db_path: str, chat_id: str) -> str:
    return f"{db_path}{REF_SEP}{chat_id}"


def parse_ref(ref: str) -> tuple[str, str]:
    db, _, cid = ref.rpartition(REF_SEP)
    if not db:
        raise ValueError(f"非法 Qwen 转录引用（应形如 <db>#<chat_id>）：{ref!r}")
    return db, cid


def text_of(parts) -> str:
    """只取文本块：`tool-*`（工具）与 `error`（报错）都不是对话内容。"""
    if isinstance(parts, str):
        try:
            parts = json.loads(parts or "[]")
        except Exception:
            return ""
    out = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type") or ""
        if t.startswith("tool") or t == "error":
            continue
        if isinstance(p.get("text"), str) and p["text"].strip():
            out.append(p["text"])
    return "\n".join(out)


class QwenAdapter(TranscriptionAdapter):
    """读写 QwenWorkCN 的会话库。"""

    name = "qwen"

    def __init__(self) -> None:
        self.DB_PATH = os.path.expanduser("~/" + DB_REL)
        self._pending: dict[str, list[dict]] = {}
        self._src_map: dict[str, str] = {}

    # ---------------------------------------------------------------- 连接

    def _connect(self, db: str | None = None, readonly: bool = False) -> sqlite3.Connection:
        path = db or self.DB_PATH
        con = sqlite3.connect(f"file:{path}?mode=ro" if readonly else path, uri=readonly)
        con.row_factory = sqlite3.Row
        return con

    def new_session_id(self) -> str:
        """产品 id 形态：`mw` + 14 位小写字母数字（实测 `mw29hqwj96fcd8fk`）。"""
        return "mw" + "".join(random.choice(_ALPHABET) for _ in range(14))

    def is_available(self) -> bool:
        if not os.path.exists(self.DB_PATH):
            return False
        try:
            con = self._connect(readonly=True)
            try:
                return con.execute(
                    "select 1 from sqlite_master where type='table' and name='chats'").fetchone() is not None
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def storage_root(self) -> Optional[str]:
        return os.path.dirname(self.DB_PATH) if os.path.exists(self.DB_PATH) else None

    # ---------------------------------------------------------------- 列会话

    def list_all_sessions(self) -> list[tuple[str, str]]:
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "select id from chats where archived_at is null and deleted_at is null "
                "order by updated_at desc").fetchall()
            return [(make_ref(self.DB_PATH, r["id"]), r["id"]) for r in rows]
        finally:
            con.close()

    def resolve_session(self, session_ref: str) -> str:
        if session_ref and session_ref not in ("current", "latest", "latest-working"):
            return session_ref
        rows = self.list_all_sessions()
        if not rows:
            raise SystemExit("Qwen 库里没有会话")
        return rows[0][1]

    def find_transcript(self, chat_id: str) -> tuple[Optional[str], Optional[str]]:
        con = self._connect(readonly=True)
        try:
            row = con.execute("select id, worktree_path, output_directory from chats where id=?",
                              (chat_id,)).fetchone()
        finally:
            con.close()
        if not row:
            return None, None
        return make_ref(self.DB_PATH, row["id"]), (row["worktree_path"] or row["output_directory"] or "")

    def load_session_meta(self, chat_id: str) -> Optional[SessionMeta]:
        con = self._connect(readonly=True)
        try:
            r = con.execute("select id, name, worktree_path, output_directory, created_at "
                            "from chats where id=?", (chat_id,)).fetchone()
            if not r:
                return None
            return SessionMeta(id=r["id"], title=r["name"] or "",
                               cwd=r["worktree_path"] or r["output_directory"] or "",
                               created_at=r["created_at"])
        finally:
            con.close()

    def sort_key(self, ref: str) -> float:
        db, cid = parse_ref(ref)
        con = self._connect(db, readonly=True)
        try:
            row = con.execute("select updated_at from chats where id=?", (cid,)).fetchone()
            return float(row["updated_at"] or 0) if row else 0.0
        finally:
            con.close()

    # ---------------------------------------------------------------- 读行

    def read_lines(self, ref: str) -> list[dict]:
        if ref in self._pending:
            return self._pending[ref]
        db, cid = parse_ref(ref)
        con = self._connect(db, readonly=True)
        try:
            rows = con.execute(
                "select id, message_id, chat_id, sub_chat_id, sequence, role, parts, metadata, "
                "searchable_text, created_at, updated_at from messages "
                "where chat_id=? order by sequence", (cid,)).fetchall()
            out = []
            for r in rows:
                o = dict(r)
                try:
                    o["parts"] = json.loads(o.get("parts") or "[]")
                except Exception:
                    o["parts"] = []
                try:
                    o["metadata"] = json.loads(o.get("metadata") or "{}")
                except Exception:
                    o["metadata"] = {}
                out.append(o)
            return out
        finally:
            con.close()

    def read_raw(self, ref: str) -> str:
        lines = self.read_lines(ref)
        return "\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + ("\n" if lines else "")

    def is_user_message(self, obj: dict) -> bool:
        return obj.get("role") == "user"

    def is_assistant_message(self, obj: dict) -> bool:
        return obj.get("role") == "assistant"

    def get_text(self, obj: dict) -> str:
        return text_of(obj.get("parts"))

    # ---------------------------------------------------------------- 写

    def branch_target(self, src_ref: str, new_id: str) -> str:
        db, _cid = parse_ref(src_ref)
        dst = make_ref(db, new_id)
        self._src_map[dst] = src_ref
        return dst

    def write_branch(self, tmp_ref: str, lines: list[dict]) -> None:
        self._pending[tmp_ref] = [dict(o) for o in lines]

    def rewrite_ids(self, lines: list[dict], old_id: str, new_id: str) -> tuple[list[dict], int]:
        n = 0
        out = []
        for o in lines:
            o = dict(o)
            if o.get("chat_id") == old_id:
                o["chat_id"] = new_id
                n += 1
            out.append(o)
        return out, n

    def extract_title_hint(self, lines: list[dict]) -> str:
        for o in reversed(lines):
            if self.is_user_message(o):
                t = " ".join((self.get_text(o) or "").split())
                if t:
                    return t[:40]
        return ""

    def _pending_of(self, tmp_ref: str) -> list[dict]:
        lines = self._pending.pop(tmp_ref, None)
        if lines is None:
            raise RuntimeError(f"finalize 找不到待落位内容：{tmp_ref}")
        return lines

    def _insert(self, con, table: str, vals: dict) -> None:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        use = [c for c in cols if c in vals]
        con.execute(f"insert into {table} ({','.join(use)}) values ({','.join('?' * len(use))})",
                    [vals[c] for c in use])

    def finalize_branch(self, tmp_ref: str, dst_ref: str) -> None:
        """一个事务写 chats + sub_chats + messages（三张表都写，少一张产品就认不全）。"""
        lines = self._pending_of(tmp_ref)
        db, new_id = parse_ref(dst_ref)
        src_ref = self._src_map.pop(dst_ref, "")
        if not src_ref:
            raise RuntimeError(f"finalize 缺少源引用（branch_target 未被调用？）：{dst_ref}")
        _db, src_id = parse_ref(src_ref)

        now_s = int(time.time())
        con = self._connect(db)
        try:
            con.execute("BEGIN")
            tpl = con.execute("select * from chats where id=?", (src_id,)).fetchone()
            if tpl is None:
                raise RuntimeError(f"找不到源会话行：{src_id}")
            stamps = [int(o.get("created_at") or now_s) for o in lines] or [now_s]

            chat = dict(tpl)
            chat.update({
                "id": new_id,
                "created_at": stamps[0], "updated_at": stamps[-1],
                "archived_at": None, "deleted_at": None,
                "source_chat_id": src_id,          # 打分支：记下从哪条分出来的
                "pr_url": None, "pr_number": None,
            })
            self._insert(con, "chats", chat)

            tsub = con.execute("select * from sub_chats where chat_id=? order by rowid desc limit 1",
                              (src_id,)).fetchone()
            if tsub is None:
                raise RuntimeError(f"源会话没有 sub_chat，无法照抄列默认值：{src_id}")
            sub = dict(tsub)
            sub.update({
                "id": self.new_session_id(), "chat_id": new_id,
                "name": chat.get("name") or "",
                "session_id": f"{random.getrandbits(32):08x}-{random.getrandbits(16):04x}-"
                              f"{random.getrandbits(16):04x}-{random.getrandbits(16):04x}-"
                              f"{random.getrandbits(48):012x}",
                "messages": "[]",
                "created_at": stamps[0], "updated_at": stamps[-1],
                "stream_id": None,
            })
            self._insert(con, "sub_chats", sub)

            for seq, o in enumerate(lines):
                text = text_of(o.get("parts"))
                ts = int(o.get("created_at") or stamps[0])
                self._insert(con, "messages", {
                    "id": self.new_session_id(), "message_id": self.new_session_id(),
                    "chat_id": new_id, "sub_chat_id": sub["id"],
                    "sequence": seq, "role": o.get("role") or "user",
                    "parts": json.dumps(o.get("parts") or [], ensure_ascii=False),
                    "metadata": json.dumps(o.get("metadata") or {}, ensure_ascii=False),
                    # ⭐ 触发器只认这一列；不填 = 消息可见但搜不到
                    "searchable_text": text,
                    "search_status": "ready",
                    "created_at": ts, "updated_at": ts,
                })
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def register_branch(self, src, new_id: str, dst_path: str, name: str,
                        parent_id: str | None = None, at_seq: int | None = None,
                        prefix_fp: str | None = None) -> None:
        """Qwen 不需要额外登记：`chats` 行本身就能被界面看到。只把标题写进去。"""
        db, cid = parse_ref(dst_path) if REF_SEP in dst_path else (self.DB_PATH, new_id)
        con = self._connect(db)
        try:
            con.execute("update chats set name=? where id=?", (name or "", cid))
            con.execute("update sub_chats set name=? where chat_id=?", (name or "", cid))
            con.commit()
        finally:
            con.close()

    def unregister_branch(self, new_id: str) -> None:
        con = self._connect()
        try:
            con.execute("delete from messages where chat_id=?", (new_id,))
            con.execute("delete from sub_chats where chat_id=?", (new_id,))
            con.execute("delete from chats where id=?", (new_id,))
            con.commit()
        finally:
            con.close()

    def list_branches(self, cwd: str | None = None) -> list[SessionMeta]:
        out = []
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "select id, name, worktree_path, output_directory, source_chat_id, created_at "
                "from chats where source_chat_id is not null and source_chat_id != '' "
                "order by created_at desc").fetchall()
            for r in rows:
                p = r["worktree_path"] or r["output_directory"] or ""
                if cwd and p != cwd:
                    continue
                out.append(SessionMeta(id=r["id"], title=r["name"] or "", cwd=p,
                                       parent_id=r["source_chat_id"] or "",
                                       created_at=r["created_at"]))
        finally:
            con.close()
        return out

    def backup_transcript(self, ref: str, backups_dir: str) -> str:
        """备份整个库（SQLite 官方 backup，避免丢 -wal 里的事务）。"""
        db, _cid = parse_ref(ref)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(backups_dir, ts)
        os.makedirs(backup_dir, exist_ok=True)
        dst = os.path.join(backup_dir, os.path.basename(db))
        src = sqlite3.connect(db)
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
        return backup_dir

"""OpenCode 适配器（第七个产品）：`opencode.db` 的**新一代**存储。

实测基线：OpenCode 1.14.28 / 桌面客户端 2.2.7，`~/.local/share/opencode/opencode.db`。

## 这个产品有三件与别人不同的事，都实测过

1. **两代存储并存**：旧 `session`/`message`/`part` 与新 `session_v2`/`session_message`。
   新建会话**只在新一代**（基准会话在旧表里 0 行）⇒ **打分支只写新一代**。
2. **必须有 `project` 行**，否则 `session list` 空白（它按目录→项目过滤）。
   `project.id` 算不出来（13 个已知行、7 种哈希变体 0 命中，是随机 id），
   但**打分支不需要造它**——分支与源会话同目录同项目，直接沿用源行的 `project_id`。
3. **`event_sequence` 发号器**：产品靠它给新消息发号。不补这一行，产品从低位发号、
   撞上已有序号 ⇒ 顺序乱 ⇒ 界面表现是"输入了新消息一直转圈（工作中）"。
   真实会话的发号器总 ≥ 已用最大 seq（实测差值 0~34），这里补 `max+1`。

行形状（“行”= 一条 `session_message` 记录，dict）：

    {"id": ..., "session_id": ..., "type": "user|assistant|idle|…",
     "seq": 整数, "time_created": ms, "data": {...}}
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

DB_NAME = "opencode.db"
REF_SEP = "#"          # 引用形如 <db 路径>#<session_id>
_ALPHABET = string.ascii_letters + string.digits


def make_ref(db_path: str, session_id: str) -> str:
    return f"{db_path}{REF_SEP}{session_id}"


def parse_ref(ref: str) -> tuple[str, str]:
    db, _, sid = ref.rpartition(REF_SEP)
    if not db:
        raise ValueError(f"非法 OpenCode 转录引用（应形如 <db>#<session_id>）：{ref!r}")
    return db, sid


def _dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)


class OpenCodeAdapter(TranscriptionAdapter):
    """读写 OpenCode 的会话库。"""

    name = "opencode"

    def __init__(self) -> None:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        self.DB_PATH = os.path.join(base, "opencode", DB_NAME)
        if not os.path.exists(self.DB_PATH):
            mac = os.path.expanduser("~/Library/Application Support/opencode/" + DB_NAME)
            if os.path.exists(mac):
                self.DB_PATH = mac
        self._pending: dict[str, list[dict]] = {}
        #: 目标引用 → 源引用。**必须在 branch_target 里记**：那时引擎才把源交给我们；
        #: 到 register_branch 已经拿不到源了（实测：退回"最新会话"会把 parent 挂错，
        #: 我第一版就把分支挂到了用户当时最新的另一条会话上）。
        self._src_map: dict[str, str] = {}

    # ---------------------------------------------------------------- 连接

    def _connect(self, db: str | None = None, readonly: bool = False) -> sqlite3.Connection:
        path = db or self.DB_PATH
        con = sqlite3.connect(f"file:{path}?mode=ro" if readonly else path, uri=readonly)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _has(con: sqlite3.Connection, table: str) -> bool:
        return con.execute(
            "select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone() is not None

    def new_session_id(self) -> str:
        """OpenCode 的会话 id 形态是 `ses_` + 24 位字母数字。

        实测：用 UUID 建出来的分支**库里明明有行**（16 条消息、parent 也挂对了），
        但 `session list` **不列**——它按自己的 id 形态认。形态不对，产品就当没这条。
        """
        return "ses_" + "".join(random.choice(_ALPHABET) for _ in range(24))

    def is_available(self) -> bool:
        if not os.path.exists(self.DB_PATH):
            return False
        try:
            con = self._connect(readonly=True)
            try:
                return self._has(con, "session_v2")
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def storage_root(self) -> Optional[str]:
        return os.path.dirname(self.DB_PATH) if os.path.exists(self.DB_PATH) else None

    # ---------------------------------------------------------------- 列会话

    def list_all_sessions(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "select id from session_v2 order by time_updated desc").fetchall()
            for r in rows:
                out.append((make_ref(self.DB_PATH, r["id"]), r["id"]))
        finally:
            con.close()
        return out

    def resolve_session(self, session_ref: str) -> str:
        if session_ref and session_ref not in ("current", "latest", "latest-working"):
            return session_ref
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "select id from session_v2 order by time_updated desc limit 1").fetchone()
        finally:
            con.close()
        if not row:
            raise SystemExit("OpenCode 库里没有会话")
        return row["id"]

    def find_transcript(self, session_id: str) -> tuple[Optional[str], Optional[str]]:
        con = self._connect(readonly=True)
        try:
            row = con.execute(
                "select id, directory from session_v2 where id=?", (session_id,)).fetchone()
        finally:
            con.close()
        if not row:
            return None, None
        return make_ref(self.DB_PATH, row["id"]), (row["directory"] or "")

    def sort_key(self, ref: str) -> float:
        db, sid = parse_ref(ref)
        con = self._connect(db, readonly=True)
        try:
            row = con.execute("select time_updated from session_v2 where id=?", (sid,)).fetchone()
            return float(row["time_updated"] or 0) if row else 0.0
        finally:
            con.close()

    # ---------------------------------------------------------------- 读行

    def read_lines(self, ref: str) -> list[dict]:
        # 待落位的临时引用要先认出来：引擎在 finalize 之前就要读它做校验
        if ref in self._pending:
            return self._pending[ref]
        db, sid = parse_ref(ref)
        con = self._connect(db, readonly=True)
        try:
            rows = con.execute(
                "select id, session_id, type, seq, time_created, time_updated, data "
                "from session_message where session_id=? order by seq", (sid,)).fetchall()
            out = []
            for r in rows:
                o = dict(r)
                try:
                    o["data"] = json.loads(o.get("data") or "{}")
                except Exception:
                    o["data"] = {}
                out.append(o)
            return out
        finally:
            con.close()

    def read_raw(self, ref: str) -> str:
        """每行一条 JSON 的等价文本（仅供引擎做行数/解析/残留校验）。"""
        lines = self.read_lines(ref)
        return "\n".join(_dumps(o) for o in lines) + ("\n" if lines else "")

    def is_user_message(self, obj: dict) -> bool:
        return obj.get("type") == "user"

    def is_assistant_message(self, obj: dict) -> bool:
        return obj.get("type") == "assistant"

    def get_text(self, obj: dict) -> str:
        data = obj.get("data") or {}
        if obj.get("type") == "user":
            return data.get("text") or ""
        blocks = data.get("content") or []
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") in ("text", "output_text"))

    # ---------------------------------------------------------------- 写

    def branch_target(self, src_ref: str, new_id: str) -> str:
        db, _sid = parse_ref(src_ref)
        dst = make_ref(db, new_id)
        self._src_map[dst] = src_ref      # finalize 用源行当模板，也用它定 parent
        return dst

    def write_branch(self, tmp_ref: str, lines: list[dict]) -> None:
        """只暂存；真正落库推迟到 finalize（＝引擎的"发布"动作）。"""
        self._pending[tmp_ref] = [dict(o) for o in lines]

    def rewrite_ids(self, lines: list[dict], old_id: str, new_id: str) -> tuple[list[dict], int]:
        """`session_message.session_id` 是唯一的会话关联列，换它即可。"""
        n = 0
        out = []
        for o in lines:
            o = dict(o)
            if o.get("session_id") == old_id:
                o["session_id"] = new_id
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

    def _src_of_dst(self, dst_ref: str) -> str:
        return self._src_map.pop(dst_ref, "")

    def finalize_branch(self, tmp_ref: str, dst_ref: str) -> None:
        """一个事务写完 `session_v2` 一行 + `session_message` 若干 + 发号器一行。"""
        lines = self._pending_of(tmp_ref)
        db, new_id = parse_ref(dst_ref)
        src_ref = self._src_map.pop(dst_ref, "")
        if not src_ref:
            raise RuntimeError(
                f"finalize 缺少源会话引用（branch_target 未被调用？）：{dst_ref}")
        _db, src_id = parse_ref(src_ref)

        now = int(__import__("time").time() * 1000)
        con = self._connect(db)
        try:
            con.execute("BEGIN")
            tpl = con.execute("select * from session_v2 where id=?", (src_id,)).fetchone()
            if tpl is None:
                raise RuntimeError(f"找不到源会话行：{src_id}")
            row = dict(tpl)
            stamps = [int(o.get("time_created") or now) for o in lines] or [now]
            row.update({
                "id": new_id,
                "title": row.get("title") or "（分支）",
                "parent_id": src_id,          # 打分支：真的挂在源会话下面
                "fork_session_id": src_id,
                "time_created": stamps[0],
                "time_updated": stamps[-1],
                "time_busy": stamps[0],
                "time_idle": stamps[-1],
                "time_viewed": stamps[-1],
                "idle_outcome": "succeeded",
                "cost": 0,
                "tokens_input": 0, "tokens_output": 0, "tokens_reasoning": 0,
                "tokens_cache_read": 0, "tokens_cache_write": 0,
            })
            cols = [r[1] for r in con.execute("PRAGMA table_info(session_v2)")]
            use = [c for c in cols if c in row]
            con.execute(
                f"insert into session_v2 ({','.join(use)}) values ({','.join('?' * len(use))})",
                [row[c] for c in use])

            seq = 0
            for i, o in enumerate(lines):
                seq = i + 1
                data = o.get("data") or {}
                con.execute(
                    "insert into session_message (id, session_id, type, seq, time_created, time_updated, data) "
                    "values (?,?,?,?,?,?,?)",
                    (f"msg_{new_id[4:14]}{seq:04d}", new_id, o.get("type") or "user", seq,
                     int(o.get("time_created") or now), int(o.get("time_updated") or now),
                     _dumps(data)))

            # ⭐ 发号器：缺它，产品会从低位发号，撞上已有序号 ⇒ "一直工作中"
            con.execute("insert or replace into event_sequence (aggregate_id, seq, owner_id) "
                        "values (?,?,NULL)", (new_id, seq + 1))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def register_branch(self, src, new_id: str, dst_path: str, name: str,
                        parent_id: str | None = None, at_seq: int | None = None,
                        prefix_fp: str | None = None) -> None:
        """OpenCode 不需要额外登记：`session_v2` 行本身就是登记。

        这里只把标题写进去（finalize 写的是源标题或占位）。
        ⚠️ 源引用**不在这里记** —— 见 `_src_map` 的注释。
        """
        db, sid = parse_ref(dst_path) if REF_SEP in dst_path else (self.DB_PATH, new_id)
        con = self._connect(db)
        try:
            con.execute("update session_v2 set title=? where id=?", (name or "", sid))
            con.commit()
        finally:
            con.close()

    def unregister_branch(self, new_id: str) -> None:
        """撤销落位副作用（分支创建失败时的回滚）。"""
        con = self._connect()
        try:
            con.execute("delete from session_v2 where id=?", (new_id,))
            con.execute("delete from session_message where session_id=?", (new_id,))
            con.execute("delete from event_sequence where aggregate_id=?", (new_id,))
            con.commit()
        finally:
            con.close()

    def list_branches(self, cwd: str | None = None) -> list[SessionMeta]:
        out = []
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "select id, title, directory, parent_id, time_created from session_v2 "
                "where parent_id is not null and parent_id != '' order by time_created desc").fetchall()
            for r in rows:
                if cwd and r["directory"] != cwd:
                    continue
                out.append(SessionMeta(id=r["id"], title=r["title"] or "", cwd=r["directory"] or "",
                                       parent_id=r["parent_id"] or "", created_at=r["time_created"]))
        finally:
            con.close()
        return out

    def load_session_meta(self, session_id: str) -> Optional[SessionMeta]:
        con = self._connect(readonly=True)
        try:
            r = con.execute(
                "select id, title, directory, parent_id, time_created from session_v2 where id=?",
                (session_id,)).fetchone()
            if not r:
                return None
            return SessionMeta(id=r["id"], title=r["title"] or "", cwd=r["directory"] or "",
                               parent_id=r["parent_id"] or "", created_at=r["time_created"])
        finally:
            con.close()

    def backup_transcript(self, ref: str, backups_dir: str) -> str:
        """备份**整个数据库**（用 SQLite 官方 backup API，保证 WAL 一致性）。

        裸 `cp opencode.db` 会丢掉还在 `-wal` 里的事务——这是 hermes 那边实测过的坑，
        同一个坑不该踩第二次。
        """
        db, _sid = parse_ref(ref)
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

    def backup_transcript(self, ref: str, backups_dir: str) -> str:
        """备份**整个数据库**（用 SQLite 官方 backup API，保证 WAL 一致性）。

        裸 `cp opencode.db` 会丢掉还在 `-wal` 里的事务——这是 hermes 那边实测过的坑，
        同一个坑不该踩第二次。
        """
        db, _sid = parse_ref(ref)
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


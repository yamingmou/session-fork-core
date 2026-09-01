"""fork_core.adapters.workbuddy — WorkBuddy 产品适配器。

存储：
  - transcript: ~/.workbuddy/projects/<workspace-slug>/<session-id>.jsonl
  - 索引:      ~/.workbuddy/workbuddy.db 的 sessions 表
消息结构（jsonl 每行）：
  - type=message + role=user/assistant + content[]（output_text/input_text 等）
  - providerData.conversationRequestId（UI "复制请求ID" 的来源）
"""

import datetime
import json
import os
import sqlite3

from ..models import SessionMeta
from .base import TranscriptionAdapter

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".workbuddy", "projects")
DB_PATH = os.path.join(HOME, ".workbuddy", "workbuddy.db")


class WorkBuddyAdapter(TranscriptionAdapter):
    name = "workbuddy"

    # ------------------------------------------------------------------
    # A. 定位
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        return db

    def resolve_session(self, session_ref: str) -> str:
        if session_ref != "current":
            return session_ref
        db = self._connect()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT id FROM sessions WHERE status='working' ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            db.close()
        if not row:
            raise SystemExit("No 'working' session found to branch from.")
        return row[0]

    def find_transcript(self, session_id: str) -> tuple[os.PathLike | None, str | None]:
        if not os.path.isdir(PROJECTS_DIR):
            return None, None
        for slug in os.listdir(PROJECTS_DIR):
            cand = os.path.join(PROJECTS_DIR, slug, session_id + ".jsonl")
            if os.path.exists(cand):
                return cand, slug
        return None, None

    # ------------------------------------------------------------------
    # B. 消息判定
    # ------------------------------------------------------------------
    def is_user_message(self, obj: dict) -> bool:
        return obj.get("type") == "message" and obj.get("role") == "user"

    def is_assistant_message(self, obj: dict) -> bool:
        return obj.get("type") == "message" and obj.get("role") == "assistant"

    def get_text(self, obj: dict) -> str:
        if obj.get("type") != "message":
            return ""
        parts = []
        for c in obj.get("content", []) or []:
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)

    def get_request_id(self, obj: dict) -> str | None:
        return (obj.get("providerData") or {}).get("conversationRequestId")

    # ------------------------------------------------------------------
    # C. 读写
    # ------------------------------------------------------------------
    def read_lines(self, path: str) -> list[dict]:
        return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

    def write_branch(self, path: str, lines: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n")

    def rewrite_ids(self, lines: list[dict], old_id: str, new_id: str) -> tuple[list[dict], int]:
        """结构化字段级替换：sessionId / output_text.text / tool_use.input /
        providerData.toolResult.content。不碰 rawContent 等原始内容。"""
        replacements = 0
        for o in lines:
            if o.get("sessionId") == old_id:
                o["sessionId"] = new_id
                replacements += 1
            for c in o.get("content", []) or []:
                if not isinstance(c, dict):
                    continue
                if isinstance(c.get("text"), str) and old_id in c["text"]:
                    c["text"] = c["text"].replace(old_id, new_id)
                    replacements += 1
                if isinstance(c.get("input"), dict):
                    for k, v in c["input"].items():
                        if isinstance(v, str) and old_id in v:
                            c["input"][k] = v.replace(old_id, new_id)
                            replacements += 1
            pd = o.get("providerData") or {}
            tr = pd.get("toolResult") or {}
            trc = tr.get("content")
            if isinstance(trc, str) and old_id in trc:
                tr["content"] = trc.replace(old_id, new_id)
                replacements += 1
            elif isinstance(trc, list):
                for item in trc:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        if old_id in item["text"]:
                            item["text"] = item["text"].replace(old_id, new_id)
                            replacements += 1
        return lines, replacements

    def extract_title_hint(self, lines: list[dict]) -> str:
        for o in reversed(lines):
            if not self.is_user_message(o):
                continue
            for c in o.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "input_text":
                    text = (c.get("text") or "").strip()
                    if not text:
                        continue
                    if text.startswith("<") or text.startswith("<!--"):
                        continue
                    first_line = text.split("\n")[0].strip()
                    if len(first_line) < 2 or first_line.startswith(("!", "/", "#", "```")):
                        continue
                    if len(first_line) > 15:
                        first_line = first_line[:15] + "…"
                    return first_line
        return ""

    # ------------------------------------------------------------------
    # D. 注册与查询
    # ------------------------------------------------------------------
    def load_session_meta(self, session_id: str) -> SessionMeta | None:
        db = self._connect()
        try:
            cur = db.cursor()
            cur.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
            row = cur.fetchone()
        finally:
            db.close()
        if not row:
            return None
        return SessionMeta(
            id=row["id"],
            title=row["custom_title"] or "",
            status=row["status"] or "",
            created_at=row["created_at"],
            cwd=row["cwd"] or "",
            parent_id="",
            extra={k: row[k] for k in row.keys() if k not in ("id", "custom_title", "status", "created_at", "cwd")},
        )

    def register_branch(self, src: SessionMeta, new_id: str, dst_path: str, name: str, parent_id: str = None) -> None:
        db = self._connect()
        try:
            cur = db.cursor()
            cols = list(src.extra.keys()) + ["id", "custom_title", "status", "created_at", "updated_at", "last_activity_at"]
            # 从 extra 恢复源行字段；extra 可能不含时间戳（在顶层），补默认
            src_row = dict(src.extra)
            src_row["id"] = src.id
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            vals = {c: src_row.get(c) for c in cols}
            vals["id"] = new_id
            vals["custom_title"] = name
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
        finally:
            db.close()

    def list_branches(self, cwd: str | None = None) -> list[SessionMeta]:
        db = self._connect()
        try:
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
        finally:
            db.close()
        branches = []
        for r in rows:
            title = r["custom_title"] or ""
            if not self.is_branch_name(title):
                continue
            ts = r["created_at"]
            if isinstance(ts, int) and ts > 1e12:
                ts = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
            elif isinstance(ts, int):
                ts = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            branches.append(
                SessionMeta(id=r["id"], title=title, status=r["status"], created_at=ts, cwd=r["cwd"])
            )
        return branches

    def is_branch_name(self, title: str) -> bool:
        return ("分支" in title) or ("·" in title) or ("fork" in title.lower())

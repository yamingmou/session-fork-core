"""fork_core.adapters.workbuddy — WorkBuddy 产品适配器。

存储：
  - transcript: ~/.workbuddy/projects/<workspace-slug>/<session-id>.jsonl
  - 索引:      ~/.workbuddy/workbuddy.db 的 sessions 表（官方，只读+插入新行）
  - 谱系:      ~/.workbuddy/fork.lineage.json（旁路索引，不污染官方 schema）
消息结构（jsonl 每行）：
  - type=message + role=user/assistant + content[]（output_text/input_text 等）
  - providerData.conversationRequestId（UI "复制请求ID" 的来源）
"""

import datetime
import json
import os
import sqlite3

from ..models import SessionMeta, VerifyItem
from .base import TranscriptionAdapter

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".workbuddy", "projects")
DB_PATH = os.path.join(HOME, ".workbuddy", "workbuddy.db")
# 旁路谱系索引（不往官方 sessions 表加字段；与 Claude adapter 的 fork.branches.json 同构）
LINEAGE_PATH = os.path.join(HOME, ".workbuddy", "fork.lineage.json")


class WorkBuddyAdapter(TranscriptionAdapter):
    name = "workbuddy"

    def __init__(self):
        # 实例化时快照模块级路径（engine 用 getattr(adapter,...) 取实例属性；
        # 测试通过改模块级 + 实例化即隔离，双处一致，避免 import 快照漂移）
        self.PROJECTS_DIR = PROJECTS_DIR
        self.DB_PATH = DB_PATH
        self.LINEAGE_PATH = LINEAGE_PATH

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

    def find_session_by_request_id(self, request_id: str) -> str | None:
        """全盘反查：哪个会话含该 conversationRequestId（跨 workspace）。

        用户从 UI "复制请求 ID" 后只想打分支，不知道源会话 id——脚本自动定位。
        conversationId（复制 JSON 里）== 会话文件名 id，全盘搜 providerData 命中即可。

        歧义处理（2026-09-03 独立审查发现）：分支复制会继承源会话的 request id
        （rewrite 只换 sessionId 不剥离 conversationRequestId）——若命中的最新文件是
        分支，会把 request-id 解析到后代分支而非源会话 → 谱系错乱。
        解决：收集全部命中，优先返回**非分支**（源会话）；仅当全部命中都是分支时才返回分支。

        性能：按 mtime 新→旧搜索（用户复制的 ID 通常来自最近操作的会话），
        命中非分支即停；分支命中先暂存继续找源。
        """
        if not os.path.isdir(PROJECTS_DIR):
            return None
        # 分支集合（旁路谱系索引；_lineage_get 返回 {fork_id: {...}}）
        branch_ids: set[str] = set()
        try:
            lineage = self._lineage_get()
            branch_ids = set(lineage.keys())
        except Exception:
            pass
        # 收集全部 jsonl，按 mtime 新→旧排序
        candidates = []
        for slug in os.listdir(PROJECTS_DIR):
            d = os.path.join(PROJECTS_DIR, slug)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".jsonl"):
                    continue
                fp = os.path.join(d, fn)
                try:
                    candidates.append((os.path.getmtime(fp), fp, fn[:-6]))
                except OSError:
                    continue
        candidates.sort(reverse=True)  # 新 → 旧
        branch_hit = None
        for _, fp, sid in candidates:
            try:
                for l in open(fp, encoding="utf-8"):
                    if request_id in l:
                        try:
                            o = json.loads(l)
                            pd = o.get("providerData") or {}
                            if pd.get("conversationRequestId") == request_id:
                                if sid in branch_ids:
                                    branch_hit = branch_hit or sid  # 暂存，继续找源
                                else:
                                    return sid  # 源会话命中即返
                                break
                        except Exception:
                            continue
            except Exception:
                continue
        return branch_hit  # 全部命中都是分支（用户可能确实想从分支再 fork）

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

    # 原始内容键：安全审查承诺不改写（API 原始响应/原始内容），其余字段递归全替换
    _RAW_KEYS = {"rawContent", "rawResponse", "raw", "originalContent", "original"}

    def rewrite_ids(self, lines: list[dict], old_id: str, new_id: str) -> tuple[list[dict], int]:
        """结构化字段级替换（递归 + 原始内容黑名单）。

        覆盖 sessionId / content[].text / output / arguments / argumentsDisplayText /
        toolResult.content / renderer.value / error.message 等全部可读字段；
        跳过 rawContent / rawResponse 等原始内容键（安全审查承诺不碰）。
        """
        replacements = 0

        def walk(node):
            nonlocal replacements
            if isinstance(node, str):
                if old_id in node:
                    replacements += 1
                    return node.replace(old_id, new_id)
                return node
            if isinstance(node, dict):
                out = {}
                for k, v in node.items():
                    if k in self._RAW_KEYS:
                        out[k] = v
                    else:
                        out[k] = walk(v)
                return out
            if isinstance(node, list):
                return [walk(v) for v in node]
            return node

        return [walk(o) for o in lines], replacements

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

    def register_branch(self, src: SessionMeta, new_id: str, dst_path: str, name: str, parent_id: str = None, at_seq: int = None) -> None:
        db = self._connect()
        try:
            cur = db.cursor()
            cols = list(src.extra.keys()) + ["id", "custom_title", "status", "created_at", "updated_at", "last_activity_at"]
            # cwd 在 SessionMeta 顶层而非 extra（load_session_meta 排除），但 sessions.cwd NOT NULL —— 必须显式补列
            if "cwd" not in cols:
                cols.append("cwd")
            # 从 extra 恢复源行字段；extra 可能不含时间戳（在顶层），补默认
            src_row = dict(src.extra)
            src_row["id"] = src.id
            src_row["cwd"] = src.cwd or ""
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
        # 旁路谱系索引（不污染官方 sessions 表）
        self._lineage_add(new_id, name, parent_id or src.id, at_seq, src.cwd or "")

    # ------------------------------------------------------------------
    # 旁路谱系索引（fork.lineage.json）
    # ------------------------------------------------------------------
    def _lineage_read(self) -> dict:
        if os.path.exists(LINEAGE_PATH):
            try:
                return json.load(open(LINEAGE_PATH, encoding="utf-8"))
            except Exception:
                pass
        return {"forks": []}

    def _lineage_write(self, data: dict) -> None:
        os.makedirs(os.path.dirname(LINEAGE_PATH), exist_ok=True)
        with open(LINEAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _lineage_add(self, new_id: str, name: str, parent_id: str, at_seq: int, cwd: str) -> None:
        data = self._lineage_read()
        data["forks"].append(
            {
                "id": new_id,
                "name": name,
                "parent_id": parent_id,
                "at_seq": at_seq,
                "cwd": cwd,
                "created_at": int(datetime.datetime.now().timestamp() * 1000),
            }
        )
        self._lineage_write(data)

    def _lineage_get(self) -> dict:
        """返回 {fork_id: {parent_id, at_seq, name, cwd, created_at}}。"""
        data = self._lineage_read()
        return {f["id"]: f for f in data.get("forks", [])}

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
        # 补谱系（parent_id / at_seq 来自旁路索引）
        lineage = self._lineage_get()
        for b in branches:
            f = lineage.get(b.id)
            if f:
                b.parent_id = f.get("parent_id", "")
                b.extra["at_seq"] = f.get("at_seq")
        return branches

    def lineage_tree(self, cwd: str | None = None) -> list[SessionMeta]:
        """返回含谱系的全部会话（含非分支的父会话），供树形展示。

        按 parent_id 链组织：父会话 → 子分支 → 孙分支。
        """
        lineage = self._lineage_get()
        if not lineage:
            return []
        # 收集所有出现在谱系里的 id（fork + parent）
        ids = set()
        for f in lineage.values():
            ids.add(f["id"])
            if f.get("parent_id"):
                ids.add(f["parent_id"])
        metas = []
        for sid in ids:
            m = self.load_session_meta(sid)
            if m:
                f = lineage.get(sid)
                if f:
                    m.parent_id = f.get("parent_id", "")
                    m.extra["at_seq"] = f.get("at_seq")
                metas.append(m)
        # 按创建时间排序（父会话在前）
        metas.sort(key=lambda m: (m.created_at is None, m.created_at or 0))
        return metas

    def is_branch_name(self, title: str) -> bool:
        return ("分支" in title) or ("·" in title) or ("fork" in title.lower())

    def verify_storage(self) -> list[VerifyItem]:
        """存储层体检：数据库存在性 + sessions 表关键 NOT NULL 约束（真库对齐）。

        约束集与真库 ~/.workbuddy/workbuddy.db 对齐——v2.2.0 cwd NOT NULL 崩溃
        的自动拦截器：约束缺失 → FAIL，防止测试库松约束问题再漏到线上。
        """
        items = []
        db_ok = os.path.exists(DB_PATH)
        items.append(VerifyItem("数据库", "L2", db_ok, DB_PATH if db_ok else f"缺失（{DB_PATH}）"))
        if db_ok:
            try:
                conn = self._connect()
                cols = {
                    r["name"]: r["notnull"]
                    for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
                }
                conn.close()
                required = {"cwd", "user_id", "status", "created_at", "updated_at", "is_playground"}
                missing = sorted(c for c in required if not cols.get(c))
                items.append(VerifyItem(
                    "sessions schema", "L2", not missing,
                    "关键 NOT NULL 约束齐全" if not missing else f"缺 NOT NULL 约束: {missing}",
                ))
            except Exception as e:
                items.append(VerifyItem("sessions schema", "L2", False, str(e)))
        projects_ok = os.path.isdir(PROJECTS_DIR)
        items.append(VerifyItem(
            "transcript 目录", "L2", projects_ok,
            PROJECTS_DIR if projects_ok else "缺失",
        ))
        return items

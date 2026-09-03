"""fork_core.adapters.claude_code — Claude Code 产品适配器（验证用，不宣传）。

存储：
  - transcript: ~/.claude/projects/<slug>/<session-id>.jsonl
    （slug 由工作区绝对路径转换：/ → -，如 /Users/me/app → -Users-me-app）
  - 无数据库；会话即文件 + ~/.claude/projects.json / history.jsonl 索引
消息结构（jsonl 每行）：
  - uuid / parentUuid（树状链）/ sessionId / type(user|assistant|system|summary)
  - message.content[]（text / tool_use / tool_result 块）

注：此 adapter 仅用于架构验证（读/定位/重写），register_branch 采用
projects.json 旁路索引（fork.branches.json），不污染 Claude Code 自身索引。
"""

import json
import os

from ..models import SessionMeta, VerifyItem
from .base import TranscriptionAdapter

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
# 旁路分支索引（验证用，不写回 Claude Code 的 projects.json）
BRANCH_INDEX = os.path.join(CLAUDE_DIR, "fork.branches.json")


class ClaudeCodeAdapter(TranscriptionAdapter):
    name = "claude-code"

    def __init__(self):
        # 实例化时快照模块级路径（engine getattr 用实例属性，测试可隔离）
        self.PROJECTS_DIR = PROJECTS_DIR
        self.CLAUDE_DIR = CLAUDE_DIR
        self.BRANCH_INDEX = BRANCH_INDEX

    # ------------------------------------------------------------------
    # A. 定位
    # ------------------------------------------------------------------
    def resolve_session(self, session_ref: str) -> str:
        if session_ref != "current":
            return session_ref
        # Claude 无全局"working"概念；取最近修改的 transcript
        newest, newest_ts = None, -1
        if os.path.isdir(PROJECTS_DIR):
            for slug in os.listdir(PROJECTS_DIR):
                p = os.path.join(PROJECTS_DIR, slug)
                if not os.path.isdir(p):
                    continue
                for fn in os.listdir(p):
                    if not fn.endswith(".jsonl"):
                        continue
                    fp = os.path.join(p, fn)
                    ts = os.path.getmtime(fp)
                    if ts > newest_ts:
                        newest, newest_ts = fn[:-6], ts
        if not newest:
            raise SystemExit("No Claude Code transcript found.")
        return newest

    def find_transcript(self, session_id: str) -> tuple[str | None, str | None]:
        # 安全校验（2026-09-03）：拒绝路径穿越 + containment 兜底
        if not session_id or "/" in session_id or "\\" in session_id or session_id in (".", ".."):
            return None, None
        if not os.path.isdir(PROJECTS_DIR):
            return None, None
        for slug in os.listdir(PROJECTS_DIR):
            cand = os.path.join(PROJECTS_DIR, slug, session_id + ".jsonl")
            if os.path.exists(cand):
                try:
                    if os.path.realpath(cand).startswith(os.path.realpath(PROJECTS_DIR) + os.sep):
                        return cand, slug
                except Exception:
                    continue
        return None, None

    # ------------------------------------------------------------------
    # B. 消息判定
    # ------------------------------------------------------------------
    def is_user_message(self, obj: dict) -> bool:
        return obj.get("type") == "user"

    def is_assistant_message(self, obj: dict) -> bool:
        return obj.get("type") == "assistant"

    def get_text(self, obj: dict) -> str:
        if obj.get("type") not in ("user", "assistant"):
            return ""
        msg = obj.get("message") or {}
        parts = []
        for c in msg.get("content", []) or []:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
        return "\n".join(parts)

    def get_request_id(self, obj: dict) -> str | None:
        # Claude Code 无 conversationRequestId；用 uuid 作为精确标识的等价物
        return obj.get("uuid")

    # ------------------------------------------------------------------
    # C. 读写
    # ------------------------------------------------------------------
    def read_lines(self, path: str) -> list[dict]:
        return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

    def write_branch(self, path: str, lines: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n")

    # 原始内容键：不改写（API 原始响应/原始内容），其余字段递归全替换
    _RAW_KEYS = {"rawContent", "rawResponse", "raw", "originalContent", "original"}

    def rewrite_ids(self, lines: list[dict], old_id: str, new_id: str) -> tuple[list[dict], int]:
        """结构化字段级替换（递归 + 原始内容黑名单）。

        覆盖 sessionId / message.content[].text / tool_use.input /
        tool_result.content 等全部可读字段；跳过 rawContent / rawResponse 等
        原始内容键（与 WorkBuddy adapter 机制一致）。
        注意：uuid / parentUuid 是消息级标识，跨会话无冲突，不替换。
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
            text = self.get_text(o).strip()
            if not text or text.startswith(("<", "<!--")):
                continue
            first_line = text.split("\n")[0].strip()
            if len(first_line) < 2 or first_line.startswith(("!", "/", "#", "```")):
                continue
            if len(first_line) > 15:
                first_line = first_line[:15] + "…"
            return first_line
        return ""

    # ------------------------------------------------------------------
    # D. 注册与查询（旁路索引，不污染 Claude Code 自身）
    # ------------------------------------------------------------------
    def load_session_meta(self, session_id: str) -> SessionMeta | None:
        path, slug = self.find_transcript(session_id)
        if not path:
            return None
        ts = os.path.getmtime(path)
        return SessionMeta(id=session_id, title="", status="", created_at=ts, cwd=slug or "")

    def _read_index(self) -> dict:
        if os.path.exists(BRANCH_INDEX):
            try:
                return json.load(open(BRANCH_INDEX, encoding="utf-8"))
            except Exception:
                pass
        return {"branches": []}

    def _write_index(self, data: dict) -> None:
        os.makedirs(CLAUDE_DIR, exist_ok=True)
        with open(BRANCH_INDEX, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def register_branch(self, src: SessionMeta, new_id: str, dst_path: str, name: str, parent_id: str = None, at_seq: int = None) -> None:
        data = self._read_index()
        data["branches"].append(
            {
                "id": new_id,
                "name": name,
                "parent_id": parent_id or src.id,
                "source_id": src.id,
                "path": dst_path,
                "at_seq": at_seq,
                "created_at": src.created_at,
            }
        )
        self._write_index(data)

    def list_branches(self, cwd: str | None = None) -> list[SessionMeta]:
        data = self._read_index()
        branches = []
        for b in data.get("branches", []):
            branches.append(
                SessionMeta(
                    id=b.get("id", ""),
                    title=b.get("name", ""),
                    status="",
                    created_at=b.get("created_at"),
                    cwd=cwd or "",
                    parent_id=b.get("parent_id", ""),
                )
            )
        return branches

    def is_branch_name(self, title: str) -> bool:
        return True  # Claude 分支在旁路索引里显式记录，不需要标题启发式

    def verify_storage(self) -> list[VerifyItem]:
        """存储层体检：~/.claude/projects/ 存在性 + 真实会话数。

        会话数 0 → L1（无真实数据，仅 fixture 级）；>0 → L2 可做真库验证。
        """
        n = 0
        ok = os.path.isdir(PROJECTS_DIR)
        if ok:
            for slug in os.listdir(PROJECTS_DIR):
                p = os.path.join(PROJECTS_DIR, slug)
                if os.path.isdir(p):
                    n += len([f for f in os.listdir(p) if f.endswith(".jsonl")])
        level = "L2" if n else "L1"
        items = [VerifyItem(
            "transcript 目录", level, ok,
            f"{PROJECTS_DIR}（{n} 个会话）" if ok else "缺失（无 Claude CLI 会话——请先跑 claude 产生会话）",
        )]
        return items

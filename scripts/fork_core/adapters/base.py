"""fork_core.adapters.base — TranscriptionAdapter 接口定义。

通用引擎只依赖本接口；每个产品（WorkBuddy / Claude Code / Codex ...）
实现一个 adapter，把「产品特有存储格式」翻译成统一契约。

接口分四组：
  A. 定位     — find_transcript / resolve_session
  B. 消息判定 — is_user_message / is_assistant_message / get_text / get_request_id
  C. 读写     — read_lines / write_branch / rewrite_ids / extract_title_hint
  D. 注册     — load_session_meta / register_branch / list_branches / is_branch_name

所有方法都接收 engine 传入的上下文，不依赖全局状态。
"""

from typing import Any, Optional

from ..models import SessionMeta


class TranscriptionAdapter:
    """接口基类。子类必须实现全部方法。"""

    # 产品名（如 "workbuddy" / "claude-code"）
    name: str = "base"

    # ------------------------------------------------------------------
    # A. 定位
    # ------------------------------------------------------------------
    def find_transcript(self, session_id: str) -> tuple[Optional[str], Optional[str]]:
        """定位会话 transcript 文件。返回 (path, workspace_slug)；找不到返回 (None, None)。"""
        raise NotImplementedError

    def resolve_session(self, session_ref: str) -> str:
        """把 'current' 解析为具体会话 id；否则原样返回。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # B. 消息判定（供通用截断点定位逻辑使用）
    # ------------------------------------------------------------------
    def is_user_message(self, obj: dict) -> bool:
        """该行是否是 user 消息。"""
        raise NotImplementedError

    def is_assistant_message(self, obj: dict) -> bool:
        """该行是否是 assistant 消息。"""
        raise NotImplementedError

    def get_text(self, obj: dict) -> str:
        """提取该行的纯文本（用于 --match 匹配、output_text 完整性判断、标题摘要）。"""
        raise NotImplementedError

    def get_request_id(self, obj: dict) -> Optional[str]:
        """提取该行的 conversationRequestId（用于 --request-id 精确截断）；无则 None。"""
        return None

    # ------------------------------------------------------------------
    # C. 读写
    # ------------------------------------------------------------------
    def read_lines(self, path: str) -> list[dict]:
        """读 jsonl → list[dict]。"""
        raise NotImplementedError

    def write_branch(self, path: str, lines: list[dict]) -> None:
        """把分支行写入文件（引擎已处理好截断与 id 替换）。"""
        raise NotImplementedError

    def rewrite_ids(self, lines: list[dict], old_id: str, new_id: str) -> tuple[list[dict], int]:
        """结构化字段级 id 替换。返回 (新 lines, 替换次数)。"""
        raise NotImplementedError

    def extract_title_hint(self, lines: list[dict]) -> str:
        """从最后一条 user 消息提取分支名摘要；无则返回空串。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # D. 注册与查询
    # ------------------------------------------------------------------
    def load_session_meta(self, session_id: str) -> Optional[SessionMeta]:
        """读取会话元数据。"""
        raise NotImplementedError

    def register_branch(
        self,
        src: SessionMeta,
        new_id: str,
        dst_path: str,
        name: str,
        parent_id: Optional[str] = None,
        at_seq: Optional[int] = None,
    ) -> None:
        """在会话索引中注册新分支（各产品不同：数据库插入 / 索引文件 / 纯文件）。

        at_seq = 截断点（会话级分支的"快照点"，谱系可回/可审计）。
        """
        raise NotImplementedError

    def list_branches(self, cwd: Optional[str] = None) -> list[SessionMeta]:
        """列出当前 workspace 的分支（含状态/时间）。"""
        raise NotImplementedError

    def is_branch_name(self, title: str) -> bool:
        """判断标题是否看起来像分支（用于 list 过滤）。"""
        raise NotImplementedError

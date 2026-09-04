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

import os
from typing import Any, Optional

from ..models import SessionMeta, VerifyItem


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

    def find_session_by_request_id(self, request_id: str) -> Optional[str]:
        """全盘反查含该 conversationRequestId 的会话（跨 workspace）。可选实现。

        用途：用户复制 UI 的"请求 ID"后打分支，但不知道源会话 id——
        引擎在 request-id 模式下优先调用本方法自动定位源会话。
        默认返回 None（不支持反查的产品，需用户显式 --session）。
        """
        return None

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

    def unregister_branch(self, new_id: str) -> None:
        """撤销 register_branch 的副作用（db 行 + 谱系条目）。

        引擎层 register 失败时回滚用（2026-09-04 评审：回滚应撤销注册副作用，
        而不是删已验证的产物文件）。adapter 如无副作用/无法撤销则 no-op。
        """
        return None

    def list_branches(self, cwd: Optional[str] = None) -> list[SessionMeta]:
        """列出当前 workspace 的分支（含状态/时间）。"""
        raise NotImplementedError

    def is_branch_name(self, title: str) -> bool:
        """判断标题是否看起来像分支（用于 list 过滤）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # E. 体检（fork --verify / --doctor 用，可选覆盖）
    # ------------------------------------------------------------------
    def verify_storage(self) -> list[VerifyItem]:
        """存储层体检（存在性/可写）。默认实现只查 PROJECTS_DIR 存在性。

        产品 adapter 应覆盖：数据库/schema 约束/真实会话数等（返回 VerifyItem 列表）。
        """
        items = []
        projects = getattr(self, "PROJECTS_DIR", None)
        ok = bool(projects) and os.path.isdir(projects) if projects else False
        items.append(VerifyItem(
            "transcript 目录", "L1", ok,
            str(projects) if ok else "缺失（无会话存储）",
        ))
        return items

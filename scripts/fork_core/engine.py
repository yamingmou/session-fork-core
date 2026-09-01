"""fork_core.engine — 跨产品通用的 fork 引擎。

不依赖任何产品细节，只通过 TranscriptionAdapter 接口与具体产品交互。
WorkBuddy / Claude Code / Codex 的差异全部被 adapter 吸收。
"""

import json
import os
import shutil
import time
import uuid

from .adapters.base import TranscriptionAdapter
from .models import ForkResult, SessionMeta

# 备份目录默认 ~/.workbuddy/backups（可通过环境变量覆盖）
DEFAULT_BACKUPS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "backups")


def _load_lines(path: str) -> list[dict]:
    """读 jsonl 文件为 dict 列表（跳过空行/坏行）。"""
    lines = []
    for l in open(path, encoding="utf-8"):
        l = l.strip()
        if not l:
            continue
        try:
            lines.append(json.loads(l))
        except Exception:
            continue  # 坏行跳过（engine 不负责格式校验，adapter 判定时忽略）
    return lines


def locate_last_reply(adapter, lines: list[dict]) -> tuple[int, int]:
    """DEFAULT 模式：截断点 = 最后一条完整 assistant 回复的末尾（上一轮输出结束）。

    语义：截到"文件末尾之前最后一条有文本的 assistant 回复"。
    - 主会话（用户刚发"打分支"，末条是 user）：截到它之前的最后一条 assistant 回复；
    - 分支再 fork（分支文件末条是 assistant，用户尚未发新指令）：截到该 assistant
      ——即分支当前的全部内容都成为新 fork 的历史（快照点可回：新投影自身也可再派生）。
    """
    n = len(lines)
    for i in range(n, 0, -1):
        o = lines[i - 1]
        if adapter.is_assistant_message(o) and adapter.get_text(o).strip():
            return i, n
    raise SystemExit("No completed assistant reply found in transcript")


def locate_split_point(adapter, lines: list[dict], match_text=None, line_no=None, request_id=None) -> tuple[int, int]:
    """指定模式：--match / --line / --request-id 定位截断点。

    返回 (cut, total)；cut 是 1-based 行号（截取 lines[:cut]）。
    校验：候选行必须是 assistant 消息，且下一行是 user 消息或 EOF（完整回复边界）。
    """
    n = len(lines)
    if request_id is not None:
        cand = None
        for i, o in enumerate(lines, 1):
            if adapter.is_assistant_message(o) and adapter.get_request_id(o) == request_id:
                cand = i  # keep last match
        if cand is None:
            raise SystemExit(f"request_id not found in any assistant reply: {request_id!r}")
    elif line_no is not None:
        cand = line_no
        if not (1 <= cand <= n):
            raise SystemExit(f"--line {cand} out of range (file has {n} lines)")
    else:
        cand = None
        for i, o in enumerate(lines, 1):
            if not adapter.is_assistant_message(o):
                continue
            if match_text in adapter.get_text(o):
                cand = i  # keep last match
        if cand is None:
            raise SystemExit(f"match text not found in any assistant reply: {match_text!r}")

    # 边界校验：截断行是 assistant 且完整收尾（下一行是 user 或 EOF）
    o = lines[cand - 1]
    if not adapter.is_assistant_message(o):
        raise SystemExit(f"Split line {cand} is not an assistant message")
    if cand < n:
        nxt = lines[cand]
        if not (adapter.is_user_message(nxt) or nxt.get("type") in ("user", "message")):
            # 下一行既不是 user 消息也不是纯事件行 → 可能落在未完成回复中间
            if adapter.is_assistant_message(nxt):
                raise SystemExit(
                    f"Line {cand} is not a complete reply boundary (line {cand+1} is assistant)"
                )
    return cand, n


def backup_source(path: str, backups_dir: str = DEFAULT_BACKUPS_DIR) -> str:
    """备份源 transcript（仅源文件，不复制数据库）。返回备份目录。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(backups_dir, ts)
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))
    return backup_dir


def verify_branch(adapter, dst_path: str, new_id: str, cut: int, src_id: str) -> list[str]:
    """验证分支文件完整性。返回错误列表（空 = 通过）。"""
    errs = []
    raw = open(dst_path, encoding="utf-8").read()
    check = [l for l in raw.splitlines() if l.strip()]
    if len(check) != cut:
        errs.append(f"line count {len(check)} != {cut}")
    for i, l in enumerate(check, 1):
        try:
            o = json.loads(l)
        except Exception as e:
            errs.append(f"parse error line {i}: {e}")
            continue
        sid = o.get("sessionId") or o.get("session_id")
        if sid is not None and sid != new_id:
            errs.append(f"line {i} sessionId mismatch: {sid}")
    if src_id in raw:
        errs.append("old session id still present in raw content")
    last = json.loads(check[-1])
    if not (adapter.is_assistant_message(last) and adapter.get_text(last).strip()):
        errs.append("last line has no assistant output_text")
    return errs


def create_fork(
    adapter: TranscriptionAdapter,
    session_ref: str,
    match_text: str = None,
    line_no: int = None,
    request_id: str = None,
    name: str = None,
    dry_run: bool = False,
    backups_dir: str = DEFAULT_BACKUPS_DIR,
) -> ForkResult:
    """核心入口：创建分支。

    流程：resolve → find → locate → backup → truncate+rewrite → write → register → verify
    """
    src_id = adapter.resolve_session(session_ref)
    transcript, slug = adapter.find_transcript(src_id)
    if not transcript:
        raise SystemExit(f"Transcript not found for {src_id}")

    src_meta = adapter.load_session_meta(src_id)
    if src_meta is None:
        # 有些产品无索引（纯文件），用最小 meta
        src_meta = SessionMeta(id=src_id, cwd=slug or "")

    lines = _load_lines(transcript)
    if not lines:
        raise SystemExit(f"Transcript is empty or unreadable: {transcript}")

    if match_text or line_no or request_id:
        cut, total = locate_split_point(adapter, lines, match_text, line_no, request_id)
        how = (
            f"match={match_text!r}" if match_text
            else (f"line={line_no}" if line_no else f"request_id={request_id!r}")
        )
    else:
        cut, total = locate_last_reply(adapter, lines)
        how = "default (previous turn's output end)"

    new_id = str(uuid.uuid4())
    if name is None:
        hint = adapter.extract_title_hint(lines)
        name = f"分支·{hint}" if hint else "分支"

    backup_dir = None
    if not dry_run:
        backup_dir = backup_source(transcript, backups_dir)

    truncated = lines[:cut]
    truncated, replacements = adapter.rewrite_ids(truncated, src_id, new_id)

    dst = os.path.join(os.path.dirname(transcript), new_id + ".jsonl")
    if not dry_run:
        adapter.write_branch(dst, truncated)

    if not dry_run:
        adapter.register_branch(src_meta, new_id, dst, name, parent_id=src_meta.id, at_seq=cut)
        errs = verify_branch(adapter, dst, new_id, cut, src_id)
        if errs:
            raise SystemExit("VERIFY FAILED:\n  - " + "\n  - ".join(errs))

    return ForkResult(
        ok=True,
        src_id=src_id,
        new_id=new_id,
        name=name,
        cut=cut,
        total=total,
        how=how,
        transcript_path=transcript,
        dst_path=dst,
        backup_dir=backup_dir,
        replacements=replacements,
    )


def list_forks(adapter: TranscriptionAdapter, cwd: str = None) -> list[SessionMeta]:
    """列出分支（交给 adapter 的查询实现）。"""
    return adapter.list_branches(cwd)

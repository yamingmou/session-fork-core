"""fork_core.engine — 跨产品通用的 fork 引擎。

不依赖任何产品细节，只通过 TranscriptionAdapter 接口与具体产品交互。
WorkBuddy / Claude Code / Codex 的差异全部被 adapter 吸收。
"""

import copy
import json
import os
import stat
import shutil
import time
import uuid

from .adapters.base import TranscriptionAdapter
from .models import ForkResult, SessionMeta, VerifyItem

# 备份目录默认 ~/.workbuddy/backups（可通过环境变量覆盖）
DEFAULT_BACKUPS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "backups")


class ForkError(Exception):
    """fork-core 异常基类（库层不抛 SystemExit——那是 CLI 的事）。调用方 except ForkError 兜住全部。"""
    exit_code = 1


class ForkVerifyError(ForkError):
    """自检未通过。此时尚未产生任何外部可见副作用（verify 先于落位/登记）。"""
    exit_code = 2

    def __init__(self, errs):
        self.errs = list(errs)
        super().__init__("VERIFY FAILED:\n  - " + "\n  - ".join(self.errs))


class ForkRegisterError(ForkError):
    """文件已落位但登记失败。失败态经回滚处理后应为孤儿文件（无害，可安全重跑）。"""
    exit_code = 3


class ForkRollbackError(ForkError):
    """回滚失败——最严重：磁盘可能残留需人工处理。必须在信息里给出具体路径，绝不静默。"""
    exit_code = 4


def _safe_remove(path: str) -> bool:
    """尽力删除，返回结果而非吞异常。失败由调用方决定后果（tmp 无所谓 / dst 必须报警）。

    Windows 只读文件删除兜底：先 chmod 解只读再删（v1.2.0 曾把分支锁只读）。
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.remove(path)
            return True
        except OSError:
            return False
    except OSError:
        return False


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
    raise ForkError("No completed assistant reply found in transcript")


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
            raise ForkError(
                f"request_id not found in any assistant reply: {request_id!r}\n"
                "  Hint: 该请求 ID 可能属于其他会话/工作区——复制 JSON 里 conversationId 即源会话 ID，"
                "用 --session <conversationId> 指定（或用 --request-id 自动反查源会话）"
            )
    elif line_no is not None:
        cand = line_no
        if not (1 <= cand <= n):
            raise ForkError(f"--line {cand} out of range (file has {n} lines)")
    else:
        cand = None
        for i, o in enumerate(lines, 1):
            if not adapter.is_assistant_message(o):
                continue
            if match_text in adapter.get_text(o):
                cand = i  # keep last match
        if cand is None:
            raise ForkError(f"match text not found in any assistant reply: {match_text!r}")

    # 边界校验：截断行是 assistant 且完整收尾（下一行是 user 或 EOF）
    o = lines[cand - 1]
    if not adapter.is_assistant_message(o):
        raise ForkError(f"Split line {cand} is not an assistant message")
    if cand < n:
        nxt = lines[cand]
        if not (adapter.is_user_message(nxt) or nxt.get("type") in ("user", "message")):
            # 下一行既不是 user 消息也不是纯事件行 → 可能落在未完成回复中间
            if adapter.is_assistant_message(nxt):
                raise ForkError(
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
    """验证分支文件完整性。返回错误列表（空 = 通过）。

    残留检查用结构化遍历（_find_residue，排除 rawContent/rawResponse 黑名单字段）
    ——与替换引擎策略一致：黑名单字段保留旧 id 是合法设计，不误报。
    """
    errs = []
    raw = open(dst_path, encoding="utf-8").read()
    check = [l for l in raw.splitlines() if l.strip()]
    parsed: list[dict] = []
    last_parse_failed = False
    if len(check) != cut:
        errs.append(f"line count {len(check)} != {cut}")
    for i, l in enumerate(check, 1):
        try:
            o = json.loads(l)
        except Exception as e:
            errs.append(f"parse error line {i}: {e}")
            if i == len(check):
                last_parse_failed = True
            continue
        parsed.append(o)
        sid = o.get("sessionId") or o.get("session_id")
        if sid is not None and sid != new_id:
            errs.append(f"line {i} sessionId mismatch: {sid}")
    # 旧 id 残留检查：排除黑名单字段（rawContent/rawResponse 合法保留）
    raw_keys = getattr(adapter, "_RAW_KEYS", set())
    residue = _find_residue(parsed, src_id, raw_keys)
    if residue:
        errs.append(f"old session id still present in {len(residue)} non-raw fields: {residue[:5]}")
    # 末条完整性（末行坏行时跳过——parse error 已单独报，避免检查错行误报）
    if parsed and not last_parse_failed:
        last = parsed[-1]
        if not (adapter.is_assistant_message(last) and adapter.get_text(last).strip()):
            errs.append("last line has no assistant output_text")
    return errs


# ----------------------------------------------------------------------
# fork --verify / --doctor：真库体检（把"真库验证"从靠用户兜底变成内置强制检查）
# ----------------------------------------------------------------------

def _collect_real_transcripts(adapter, limit: int = 3) -> list[tuple[str, str, bool, str]]:
    """收集真实会话 transcript：分类为 源会话 / 分支，各自取最新 limit 个。

    返回 [(path, session_id, is_branch, parent_id_or_empty)]。
    分支识别：查谱系索引（lineage forks / branches）——分支必须用其 parent_id（源 id）
    做残留校验（分支正确时源 id 已被替换干净，若残留 = rewrite 回归）。
    """
    found: list[tuple[str, str]] = []
    projects = getattr(adapter, "PROJECTS_DIR", None)
    if not projects or not os.path.isdir(projects):
        return found
    for slug in os.listdir(projects):
        p = os.path.join(projects, slug)
        if not os.path.isdir(p):
            continue
        for fn in os.listdir(p):
            if fn.endswith(".jsonl"):
                found.append((os.path.join(p, fn), fn[:-6]))
    found.sort(key=lambda x: os.path.getmtime(x[0]), reverse=True)

    # 分支集合 + parent 映射（从旁路谱系索引；_lineage_get 返回 {fork_id: {...}}）
    branch_parent: dict[str, str] = {}
    try:
        if hasattr(adapter, "_lineage_get"):
            data = adapter._lineage_get()
            branch_parent = {fid: f.get("parent_id", "") for fid, f in data.items() if f.get("parent_id")}
        elif hasattr(adapter, "_read_index"):
            data = adapter._read_index()
            for f in data.get("branches", []):
                if f.get("id"):
                    branch_parent[f["id"]] = f.get("parent_id") or f.get("source_id") or ""
    except Exception:
        pass

    sources = [f for f in found if f[1] not in branch_parent]
    branches = [f for f in found if f[1] in branch_parent]
    result = []
    for path, sid in sources[:limit]:
        result.append((path, sid, False, ""))
    for path, sid in branches[:limit]:
        result.append((path, sid, True, branch_parent.get(sid, "")))
    return result


def _find_residue(objs: list[dict], old_id: str, raw_keys: set) -> list[str]:
    """递归找旧 id 在非黑名单字段中的残留位置（黑名单 = 原始内容键，允许含旧 id）。"""
    hits = []

    def walk(node, path):
        if isinstance(node, str):
            if old_id in node:
                hits.append(path or "(root)")
        elif isinstance(node, dict):
            for k, v in node.items():
                if k in raw_keys:
                    continue
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    for i, o in enumerate(objs, 1):
        walk(o, f"L{i}")
    return hits


def verify_environment(adapter: TranscriptionAdapter) -> list[VerifyItem]:
    """真库体检（fork --verify / --doctor）。

    检查项：
    1. 存储层（adapter.verify_storage）：数据库/transcript 目录存在性 + schema 约束
    2. 真实数据替换验证（L2）：取最新真实会话 → 定位截断点 → rewrite_ids 全量替换
       → 断言非黑名单字段零残留——这就是 v2.2.0 两个 bug 的自动拦截器
    3. 谱系索引可读（旁路 fork.lineage.json / fork.branches.json）
    """
    items = []

    # 1. 存储层
    items.extend(adapter.verify_storage() or [])

    # 2. 真实数据替换验证（L2）
    #    源会话：模拟 rewrite，验证引擎能把自身 id 清干净（黑名单除外）；
    #    分支：直接查源 id（parent_id）是否残留——分支正确时源 id 已清零，
    #          残留 = rewrite 回归或分支产物被污染（不能模拟 rewrite，会顺手修复变假绿）。
    real = _collect_real_transcripts(adapter)
    if not real:
        items.append(VerifyItem(
            "真实数据替换验证", "L1", False,
            "无真实会话可用（仅 fixture 级）。请先用产品产生会话：WorkBuddy 直接对话 / Claude Code 在终端跑 claude 命令",
        ))
    else:
        raw_keys = getattr(adapter, "_RAW_KEYS", set())
        n_src = sum(1 for r in real if not r[2])
        n_br = len(real) - n_src
        for path, sid, is_branch, parent_id in real:
            lines = _load_lines(path)
            if not lines:
                items.append(VerifyItem(f"真实数据验证 {sid[:8]}", "L2", False, "transcript 为空或不可读"))
                continue
            try:
                cut, total = locate_last_reply(adapter, lines)
            except ForkError as e:
                items.append(VerifyItem(f"截断定位 {sid[:8]}", "L2", False, str(e)))
                continue
            if is_branch:
                # 分支产物验证：源 id 必须零残留（黑名单除外）
                residue = _find_residue(lines[:cut], parent_id, raw_keys)
                ok = not residue
                detail = f"源 id {parent_id[:8]} 零残留（分支产物正确）" if ok else \
                         f"源 id 残留 {len(residue)} 处：{residue[:3]}（分支被污染/rewrite 回归）"
                items.append(VerifyItem(f"分支产物校验 {sid[:8]}", "L2", ok, detail))
            else:
                # 源会话：模拟 rewrite，验证引擎替换能力
                new_id = "verify-" + uuid.uuid4().hex[:12]
                rewritten, n = adapter.rewrite_ids(copy.deepcopy(lines[:cut]), sid, new_id)
                residue = _find_residue(rewritten, sid, raw_keys)
                ok = not residue
                detail = f"{n} 处替换，截断点 L{cut}/{total}"
                if residue:
                    detail += f"，残留 {len(residue)} 处：{residue[:3]}"
                items.append(VerifyItem(f"真实数据替换 {sid[:8]}", "L2", ok, detail))
        # 汇总项 ok = 本轮所有真实数据检查项都通过（不硬编码绿，2026-09-03 审计）
        data_items = [it for it in items if it.name.startswith(("真实数据替换", "分支产物校验", "截断定位"))]
        all_ok = bool(data_items) and all(it.ok for it in data_items)
        items.append(VerifyItem(
            "体检覆盖", "L2", all_ok,
            f"抽查最新 {n_src} 个源会话 + {n_br} 个分支"
            + ("（全部通过）" if all_ok else f"（{sum(1 for it in data_items if not it.ok)} 项失败——见上）"),
        ))

    # 3. 谱系索引
    try:
        if hasattr(adapter, "_read_index"):
            data = adapter._read_index()
            branches = data.get("branches", [])
            items.append(VerifyItem("谱系索引", "L2", True, f"可读（{len(branches)} 个分支记录）"))
        elif hasattr(adapter, "_lineage_get"):
            data = adapter._lineage_get()  # {fork_id: {...}}
            items.append(VerifyItem("谱系索引", "L2", True, f"可读（{len(data)} 个分支记录）"))
        else:
            items.append(VerifyItem("谱系索引", "L1", True, "adapter 无旁路索引（跳过）"))
    except Exception as e:
        items.append(VerifyItem("谱系索引", "L2", False, str(e)))

    return items


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

    流程：resolve → find → locate → backup → truncate+rewrite → write → verify → register
    （verify 先于 register——验证失败不留任何 db/lineage 痕迹；2026-09-04 学习 Marvis）
    """
    # request-id 模式：用户复制 UI "请求 ID" 打分支，可能不知道源会话（跨 workspace）。
    # 若 --session 是 current（未显式指定源），先全盘反查该 request-id 属于哪个会话，
    # 自动定位源——这样用户只需贴复制的 ID 就能打分支，无需理解 session 概念。
    if request_id and (not session_ref or session_ref == "current"):
        auto_src = adapter.find_session_by_request_id(request_id)
        if auto_src:
            session_ref = auto_src

    src_id = adapter.resolve_session(session_ref)
    transcript, slug = adapter.find_transcript(src_id)
    if not transcript:
        raise ForkError(f"Transcript not found for {src_id}")

    src_meta = adapter.load_session_meta(src_id)
    if src_meta is None:
        # 有些产品无索引（纯文件），用最小 meta
        src_meta = SessionMeta(id=src_id, cwd=slug or "")

    lines = _load_lines(transcript)
    if not lines:
        raise ForkError(f"Transcript is empty or unreadable: {transcript}")

    if match_text or line_no or request_id:
        # locate_split_point 找不到时自行 ForkError（带 Hint），此处不会返回 None
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
    truncated = lines[:cut]
    truncated, replacements = adapter.rewrite_ids(truncated, src_id, new_id)
    dst = os.path.join(os.path.dirname(transcript), new_id + ".jsonl")
    # L0 原子写（2026-09-04 事务化改造）：先写 .tmp（不被侧边栏扫、不在 db → 零外部可见副作用），
    # verify 校验 tmp 的磁盘字节；通过才 os.replace 落位（原子，无中间态）。
    # 自检失败/中途异常 = 只留 tmp 垃圾文件（无害）——回滚不依赖删除（L0 优于 L1）。
    tmp = f"{dst}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        if not dry_run:
            backup_dir = backup_source(transcript, backups_dir)
        adapter.write_branch(tmp, truncated)
        try:
            errs = verify_branch(adapter, tmp, new_id, cut, src_id)
        except Exception as e:
            # verify 自身异常：tmp 删不掉也无害（不在 db/侧边栏）
            _safe_remove(tmp)
            raise ForkError(f"VERIFY 执行异常：{e}") from e

        if dry_run:
            # dry-run 走完整校验路径（洞 5：dry-run 的 ok 要有真实信息量），只是不落位/不登记
            if errs:
                _safe_remove(tmp)
                raise ForkVerifyError(errs)
            _safe_remove(tmp)
            return ForkResult(
                ok=True, src_id=src_id, new_id=new_id, name=name, cut=cut, total=total,
                how=how, transcript_path=transcript, dst_path=dst, backup_dir=None,
                replacements=replacements, dry_run=True, verified=True,
            )

        if errs:
            # 尚未产生任何外部可见状态——只需不开始，不需撤销
            _safe_remove(tmp)  # 删不掉也无所谓：.tmp 不被扫
            raise ForkVerifyError(errs)

        # ── 文件落位（第一个"发布"动作；失败态优先对准孤儿文件侧）──
        os.replace(tmp, dst)
        # 读锁不再自动加（v1.4.0 起提示用户手动 chmod 444），此处只负责落位

        # ── 登记（db + lineage；内部顺序见 adapter，外层只做补救）──
        try:
            adapter.register_branch(src_meta, new_id, dst, name, parent_id=src_meta.id, at_seq=cut)
        except Exception as e:
            # register 失败（其内部已尽量同序，db/lineage 侧用 unregister 兜底清理）；
            # 文件已落位——回滚文件让失败态退回孤儿文件（无害）；删不掉必须显式报错
            unreg_err = ""
            try:
                unreg = getattr(adapter, "unregister_branch", None)
                if callable(unreg):
                    unreg(new_id)
            except Exception as re:
                unreg_err = f"\n  注册痕迹清理失败（db/lineage 可能残留 {new_id[:8]}，请人工清理）：{re}"
            if _safe_remove(dst):
                raise ForkRegisterError(f"登记失败，分支文件已回滚：{dst}{unreg_err}") from e
            raise ForkRollbackError(
                f"登记失败，且分支文件无法删除——需人工清理：\n  {dst}\n"
                f"  该文件未完整登记，可能残留 db/谱系痕迹{unreg_err}"
            ) from e
    finally:
        # 兜底清理 tmp（replace 成功后 tmp 已不存在）
        _safe_remove(tmp)

    return ForkResult(
        ok=True, src_id=src_id, new_id=new_id, name=name, cut=cut, total=total,
        how=how, transcript_path=transcript, dst_path=dst, backup_dir=backup_dir,
        replacements=replacements, verified=True,
    )


def list_forks(adapter: TranscriptionAdapter, cwd: str = None) -> list[SessionMeta]:
    """列出分支（交给 adapter 的查询实现）。"""
    return adapter.list_branches(cwd)

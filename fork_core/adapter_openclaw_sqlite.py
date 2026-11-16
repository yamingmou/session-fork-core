# role: adapter:openclaw（SQLite 后端）— 产品适配器，不含引擎逻辑
"""fork_core.adapter_openclaw_sqlite — OpenClaw ≥2026.9.x 的 SQLite 后端。

一手依据（2026-09-14 真机取证，可复跑核对）：

  1. 真机安装 `openclaw@2026.9.4`（latest，要求 Node >=24.16 <25），
     用 ollama 本地模型跑出真实会话。
  2. 落盘判据：`agents/<id>/agent/openclaw-agent.sqlite` 里
     **`transcript_events` 有 31 行、`session_transcript_active_events` 30 行，
     且 sessions/ 下没有任何 .jsonl** —— 转录确实在 SQLite。
  3. 引擎要的唯一契约 —— `(session_id, seq) → event_json` —— 与 JSONL 的
     「行序 → 条目」**完全同构**：seq=0 同样是 `{"type":"session",...}` header，
     后续是同样词汇的 `model_change` / `message` / `custom` 条目，
     同样带 `id` / `parentId` 树。所以抽象层可以做到零语义损失。

版本基线：**2026.11.3**（旧基线 2026.9.9）。2026.11.x 的差异由产品源码 / 二进制核验
（DDL 与写入投影 `bindSessionNode`、读取侧 `session-entry-read.worker` 均已对照），
体现在 ②（entry_valid 与血缘等式）和压缩事件解码（zstd）两处。

存储契约（**一个会话需要 7 张表**，缺一即不可用 —— 全部由实测确定）：

  | 表 | 作用 | 分支侧要写什么 |
  |---|---|---|
  | session_windows | 会话窗口（PRIMARY KEY session_id） | 复制源行，换 session_id / session_key |
  | transcript_events | **转录本体** (session_id,seq)→event_json | 复制 seq < cut，header.id 换新 |
  | transcript_event_identities | seq → event_id / parent_id 反规范化索引 | 随事件复制 |
  | session_transcript_active_events | **投影**：(active_position)→event_seq | 复制 event_seq < cut |
  | transcript_rewrite_watermarks | 代际水印 | 新 generation |
  | session_transcript_index_state | **投影进度**（indexed_seq / leaf_event_id / needs_rebuild） | 见下 |
  | session_nodes | session_key → current_session_id + entry_json | 新行 |

  ⚠️ 两条必知契约（② 的旧结论已按 2026.11.3 源码订正，见下）：

  ① **`session_transcript_index_state` 不能省**。漏掉它，会话**能被列出**，但运行时
     会报 `Session transcript projection is rebuilding: <id>` 并拒绝执行 ——
     因为没有投影进度，系统认为需要重建。补齐（indexed_seq = 已投影最大 seq、
     leaf_event_id、active_event_count / active_message_count 与实际行数一致）后即正常。

  ② **`session_nodes.entry_valid` 由写入方自己写 1**（2026.11.3 事实，已订正）。
     旧版本（≤2026.9.x）表上有 AFTER INSERT 触发器把 entry_valid 置 0（"待校验"），
     官方迁移 `migrateCanonicalSessionWriterValidation` 已把 legacy 触发器全部 DROP；
     2026.11.3 的 `session_nodes` **没有任何触发器**，列定义是
     `NOT NULL DEFAULT 0 CHECK IN (-1,0,1)`，官方写入投影 `bindSessionNode` **内联
     `entry_valid: 1`**。读取侧 `session-entry-read.worker` 严格
     `if (row.entry_valid !== 1 || row.pending_key !== null || row.invalid_metadata) throw
     "invalid persisted session row requires repair"`；而 `doctor` 的变换器对干净行是
     no-op（**不会**把 0 改成 1）。故本适配器直接写 1 —— 不写 1 分支必然读不出来。
     推论：也**不要**往 `session_canonical_validation_pending` 写行（进队列会挡住续跑）。

     血缘列与 `entry_json` 必须自洽（2026.11.3 新增 `validateCanonicalSessionRowEntry`，
     read / admission 两种模式都生效）：
       `parent_session_key     == entry.parentSessionKey ?? entry.spawnedBy`
       `spawned_by             == entry.spawnedBy`
       `fork_source_session_key == entry.forkSource?.sessionKey`
     故本适配器把谱系**同时**写进 entry（见 `_copy_session_rows`）。

  **产物是活的**：真机实测，Python 造出的分支被 `openclaw sessions` 列出、
  `openclaw agent --session-key` 正常续跑（事件数 20 → 35），且 OpenClaw 自己
  把该分支的 indexed_seq 维护到 34 —— 说明它已完全接管这个分支。

已知边界（如实记录）：
  - 依赖**未公开的内部表结构**，随 OpenClaw 版本变化可能失效；写入失败时应
    回退到 JSONL 后端或提示用户升级适配器。
  - **压缩事件已真解码**：OpenClaw 2026.11.3 起默认开 zstd，事件 JSON ≥1024B 落成
    `event_json = NULL, event_zstd = <zstd 字节>`，并用 `event_utf8_bytes` 记原文长度
    （读侧官方用 `coalesce(event_json, decode(event_zstd))`）。本适配器照做：解压实现
    **优先取 Python 3.14+ stdlib 的 `compression.zstd`**（零依赖），退回运行时依赖
    `zstandard`；**两级都不可用才**抛 `ForkError`（带可执行修复建议）。
    解出的字节用 `event_utf8_bytes` 校验长度（防"静默解出半截"），不符或解压失败
    一律带 `session_id` + `seq` 报错，**绝不**把 `event_json IS NULL` 当成坏行/空行。
    见 `_rows_for_session` / `zstd_support`。
  - 不维护 `session_transcript_fts*`（全文检索索引）：该表无触发器，
    缺失只影响会话内搜索，不影响会话加载与会话续跑（实测未触发问题）。
  - 不做"树路径抽取"：与 JSONL 后端一致，截断是 `seq < cut` 前缀。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid

from .adapter_base import dumps_safe, parse_jsonl
from .adapter_openclaw import OpenClawJsonlAdapter, BRANCH_KEY_PREFIX
from .engine import ForkError  # 压缩事件必须"响一声"而不是静默变坏行
from .models import SessionMeta, VerifyItem

DB_NAME = "openclaw-agent.sqlite"
DB_REL = os.path.join("agent", DB_NAME)
REF_SEP = "#"  # "<db 绝对路径>#<session_id>"


def make_ref(db_path: str, session_id: str) -> str:
    return f"{db_path}{REF_SEP}{session_id}"


def parse_ref(ref: str) -> tuple[str, str]:
    db, _, sid = ref.rpartition(REF_SEP)
    if not db:
        raise ValueError(f"非法 SQLite 转录引用（应形如 <db>#<session_id>）：{ref!r}")
    return db, sid


# ---------------------------------------------------------------------------
# zstd 解码（2026.11.x 起：≥1024B 的事件落成 event_json=NULL + event_zstd=<blob>）
# ---------------------------------------------------------------------------
# 读侧官方表达式是 `coalesce(event_json, decode(event_zstd))`，本适配器照做。
# 关键是"解码能力必须能在运行环境里真找到"——找不到就明确报错，绝不把
# event_json=NULL 当空行/坏行（那会静默产出"少了最大几条事件"的坏分支）。
#
# 探测顺序：
#   ① Python 3.14+ stdlib `compression.zstd`（PEP 784，零依赖，最优）
#   ② 第三方 `zstandard`（pyproject 的运行时依赖）
#   ③ 都没有 → ForkError（带 `pip install` 修复建议）
_ZSTD_MAX_OUTPUT = 256 * 1024 * 1024  # 单条事件解压上限（防御损坏/畸形 blob 炸内存）
# None = 尚未探测；探测后是 (实现名 or None, 一次性解压函数 or None)。
# 测试通过改这个缓存来隔离"两级都不可用"的情形，不需要真卸载包。
_ZSTD_CACHE: tuple[str | None, object | None] | None = None

ZSTD_FIX_ADVICE = 'pip install "zstandard>=0.22"'  # 报告里直接照抄就能修


def _zstd_decode_stdlib(data: bytes, limit: int) -> bytes:
    from compression import zstd as _zstd  # Python 3.14+（PEP 784）

    try:
        return _zstd.decompress(data, max_output_size=limit)
    except TypeError:  # 某些实现没有 max_output_size 关键字
        return _zstd.decompress(data)


def _zstd_decode_pkg(data: bytes, limit: int) -> bytes:
    import zstandard  # 运行时依赖（pyproject.dependencies）

    # 用**一次性**接口并显式给 max_output_size：帧头不带 content size 时，
    # zstandard 的流式/默认接口会直接抛错 —— 而不是"能解就解"。
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=limit)


def zstd_support() -> tuple[str | None, object | None]:
    """探测可用的 zstd 解压实现，返回 `(实现名, 一次性解压函数)`；都没有则 `(None, None)`。

    进程内只探一次（结果缓存）。解压函数签名为 `(data: bytes, limit: int) -> bytes`。
    """
    global _ZSTD_CACHE
    if _ZSTD_CACHE is None:
        impl: tuple[str | None, object | None] = (None, None)
        try:
            import compression.zstd  # noqa: F401  （Py3.14+ 才有）

            impl = ("stdlib compression.zstd", _zstd_decode_stdlib)
        except Exception:
            try:
                import zstandard

                impl = (f"zstandard {getattr(zstandard, '__version__', '')}".strip(),
                        _zstd_decode_pkg)
            except Exception:
                impl = (None, None)
        _ZSTD_CACHE = impl
    return _ZSTD_CACHE


class OpenClawSqliteAdapter(OpenClawJsonlAdapter):
    """OpenClaw ≥2026.9.x（SQLite 运行时）后端。

    复用 JSONL 后端的格式层（消息判定 / get_text / rewrite_ids / 标题提取 /
    旁路谱系索引），只替换介质相关部分：定位、读写、落位、注册、体检。
    """

    name = "openclaw"

    # 原始内容键（覆盖 JSONL 后端的 {"parentSession","content"}，再加 data）。
    #
    # data：**custom 条目的运行时原始记录**。真机实测，OpenClaw 会在会话中写入
    #   `customType: "openclaw:bootstrap-context:full"` 条目，其 `data` 形如
    #   `{"timestamp":…,"runId":…,"sessionId":<源会话 id>}` —— 它记录的是
    #   「这一次 run 发生在哪个会话上」，是**历史事实**。
    #   分支继承这段历史时，把 sessionId 改成新 id 等于篡改历史记录
    #   （而引擎首轮的残留检查会把它判成"源 id 残留"从而**误拒分支**，
    #   实测复现：errs=['old session id still present in 1 non-raw fields: [L25.data.sessionId]']）。
    #   与 pi 处理 parentSession / content 同一条逻辑：这些字段保留源 id 是合法设计。
    #   ⚠️ 已知风险（如实记录）：若未来某个 OpenClaw 版本改用该字段做**会话归属判断**
    #   而非历史记录，本豁免就需要收紧。当前无证据表明运行时读它定位会话
    #   （会话定位一律走 session_id 列）。
    _RAW_KEYS = {"parentSession", "content", "data"}

    def __init__(self):
        super().__init__()
        self.DB_PATH = os.path.join(self.AGENT_DIR, DB_REL)
        # write_branch → finalize_branch 之间的暂存：key 是产物 ref
        # 这样 verify 能读到"尚未对外可见"的内容，且**不留下悬挂事务**。
        self._pending: dict[str, list[dict]] = {}
        # 产物 ref → 源 ref（branch_target 时记下，finalize 时要靠它复制源行）
        self._src_of_dst: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            con = sqlite3.connect(f"file:{self.DB_PATH}?mode=ro", uri=True)
        else:
            con = sqlite3.connect(self.DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _cols(cur, table: str) -> list[str]:
        return [d[0] for d in cur.execute(f"SELECT * FROM {table} LIMIT 0").description]

    def is_available(self) -> bool:
        if not os.path.exists(self.DB_PATH):
            return False
        try:
            con = self._connect(readonly=True)
            n = con.execute("SELECT COUNT(*) FROM transcript_events").fetchone()[0]
            con.close()
            return n > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # A. 定位
    # ------------------------------------------------------------------
    def _sessions(self) -> list[tuple[str, str, int]]:
        """[(ref, session_id, updated_at)]，按更新时间倒序。"""
        if not os.path.exists(self.DB_PATH):
            return []
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "SELECT session_id, updated_at FROM session_windows ORDER BY updated_at DESC"
            ).fetchall()
        except Exception:
            rows = []
        finally:
            con.close()
        return [(make_ref(self.DB_PATH, r["session_id"]), r["session_id"], r["updated_at"] or 0) for r in rows]

    def resolve_session(self, session_ref: str) -> str:
        if session_ref != "current":
            return session_ref
        rows = self._sessions()
        if not rows:
            raise SystemExit(f"No OpenClaw session found in {self.DB_PATH}（先跑 openclaw agent 产生会话）")
        return rows[0][1]

    def find_transcript(self, session_id: str) -> tuple[str | None, str | None]:
        if not session_id or "/" in session_id or "\\" in session_id:
            return None, None
        for ref, sid, _ in self._sessions():
            if sid == session_id:
                return ref, self.AGENT_ID
        return None, None

    def find_session_by_request_id(self, request_id: str) -> str | None:
        """按**事件 id** 反查会话（transcript_event_identities.event_id）。"""
        if not os.path.exists(self.DB_PATH) or not request_id:
            return None
        con = self._connect(readonly=True)
        try:
            rows = con.execute(
                "SELECT session_id FROM transcript_event_identities WHERE event_id = ? LIMIT 2",
                (request_id,),
            ).fetchall()
        except Exception:
            rows = []
        finally:
            con.close()
        if not rows:
            return None
        branched = {b.get("id") for b in self._read_index().get("branches", [])}
        # 非分支优先（分支会继承被复制的条目 id）
        return rows[0]["session_id"] if rows[0]["session_id"] not in branched else rows[-1]["session_id"]

    def list_all_sessions(self) -> list[tuple[str, str]]:
        return [(ref, sid) for ref, sid, _ in self._sessions()]

    def is_historical_id_path(self, path: str) -> bool:
        """豁免 custom 条目 `data` 子树里的会话关联字段。

        真机实测：OpenClaw 写入 `customType: "openclaw:bootstrap-context:full"` 条目，
        其 `data` 同时含 `runId` 与 `sessionId` —— 记录"这一次 run 发生在哪个会话"，
        是**历史事实**。分支继承这段历史时不该改写它（改写 = 篡改历史）。
        路径形如 `L25.data.sessionId`，故判定"路径里含 data 段"。
        """
        return ".data." in path or path.endswith(".data")

    def sort_key(self, ref: str) -> float:
        """最近使用排序键 = session_windows.updated_at（ref 不是文件路径）。"""
        db, sid = parse_ref(ref)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT updated_at FROM session_windows WHERE session_id=?", (sid,)
                ).fetchone()
            finally:
                con.close()
            return float(row[0]) if row and row[0] else 0.0
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # C. 读写（行存储契约：(session_id, seq) → event_json）
    # ------------------------------------------------------------------
    def _rows_for_session(self, db: str, sid: str) -> list:
        """读出一个会话的全部事件，按 seq 升序返回**每条的 JSON 原文**（str）。

        2026.11.3 起产品默认开 zstd：≥1024B 的事件落成
        `event_json = NULL, event_zstd = <zstd>`，并用 `event_utf8_bytes` 记原文长度
        （表上 CHECK 约束保证 event_json / event_zstd 恰有其一）。读侧官方是
        `coalesce(event_json, decode(event_zstd))` —— 这里照做，解出来的字节再用
        `event_utf8_bytes` 校验长度（这是"没静默解出半截"的唯一判据）。

        一列都可能不存在的旧库（2026.9.x）必须照读：查询按**实际列集**拼，
        不硬写 `event_zstd` / `event_utf8_bytes`。
        """
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cols = [d[0] for d in con.execute("SELECT * FROM transcript_events LIMIT 0").description]
            sel = ["seq", "event_json"]
            for c in ("event_zstd", "event_utf8_bytes"):
                if c in cols:
                    sel.append(c)
            rows = con.execute(
                f"SELECT {','.join(sel)} FROM transcript_events WHERE session_id=? ORDER BY seq",
                (sid,),
            ).fetchall()
        finally:
            con.close()

        i_zstd = sel.index("event_zstd") if "event_zstd" in sel else None
        i_len = sel.index("event_utf8_bytes") if "event_utf8_bytes" in sel else None
        n_comp = sum(1 for r in rows if r[1] is None)
        dec = None
        if n_comp:
            dec = zstd_support()[1]
            if dec is None:
                raise ForkError(
                    f"该会话含 zstd 压缩事件 {n_comp} 条（event_json 为 NULL、event_zstd 有值）"
                    "—— OpenClaw 2026.11.x 默认把 ≥1024B 的事件压缩落盘，"
                    "而当前 Python 环境没有可用的 zstd 解码实现"
                    f"（stdlib 也没有：`compression.zstd` 需 Python 3.14+；当前 {sys.version.split()[0]}）。"
                    f"修复：{ZSTD_FIX_ADVICE}（正常 `pip install` 本包时会自动装上）。"
                    "为避免静默产出一个缺事件的坏分支，这里直接停下。"
                )
        out = []
        for r in rows:
            seq, raw = r[0], r[1]
            if raw is not None:
                out.append(raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace"))
                continue
            blob = r[i_zstd] if i_zstd is not None else None
            if blob is None:
                # event_json 与 event_zstd 双 NULL：违反产品自己的 CHECK 约束，是真坏行
                raise ForkError(
                    f"{sid} 的第 {seq} 条事件既没有 event_json 也没有 event_zstd"
                    "（违反 OpenClaw 表上的 CHECK 约束）⇒ 该行已损坏，无法安全读取"
                )
            expected = r[i_len] if i_len is not None else None
            out.append(self._decode_zstd_event(blob, expected, sid, seq))
        return out

    @staticmethod
    def _decode_zstd_event(blob, expected, sid: str, seq) -> str:
        """解一条 zstd 事件 → JSON 原文；解压失败/长度不符都带 session_id + seq 报错。"""
        exp = int(expected) if expected else 0
        limit = exp if exp > 0 else _ZSTD_MAX_OUTPUT
        _, dec = zstd_support()
        if dec is None:  # 调用方已拦过，这里只兜底
            raise ForkError(f"缺少 zstd 解码实现，无法读取 {sid} 的第 {seq} 条事件。修复：{ZSTD_FIX_ADVICE}")
        try:
            raw = dec(blob, limit)
        except Exception as e:
            raise ForkError(
                f"zstd 解压失败：session_id={sid} seq={seq}（{type(e).__name__}: {e}）"
                "⇒ 该事件的 event_zstd 字节已损坏或不完整，无法安全读取"
            ) from e
        if exp > 0 and len(raw) != exp:
            # event_utf8_bytes 是产品写的原文长度；不符说明解出来的不是完整原文
            raise ForkError(
                f"zstd 解压长度不符：session_id={sid} seq={seq} "
                f"event_utf8_bytes={exp} 实际解出 {len(raw)} 字节 ⇒ 事件被截断/损坏，"
                "拒绝继续（否则会静默产出内容不完整的分支）"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ForkError(
                f"zstd 解压结果不是合法 UTF-8：session_id={sid} seq={seq}（{e}）"
            ) from e

    def read_lines(self, ref: str) -> list[dict]:
        if ref in self._pending:
            return self._pending[ref]
        db, sid = parse_ref(ref)
        rows = self._rows_for_session(db, sid)
        # 逐个容错解析：DB 里也可能有坏行（半写/上游写坏），一个坏行不该让整轮体检崩
        # （防御性：本机真库实测 0 例，见 adapter_base.parse_jsonl 的证据等级说明）。
        # 坏行的**行号按"第几条事件"计**（seq 升序的序号）——与 at_seq 同一口径。
        out = []
        for text in rows:
            out.extend(parse_jsonl(text).objs)
        return out

    def read_lines_checked(self, ref: str):
        """坏行按"第几条事件"报出（本后端不走文件，故不能依赖基类的文件默认实现）。"""
        from .adapter_base import ParseResult

        if ref in self._pending:
            return ParseResult(objs=self._pending[ref], bad_lines=[])
        db, sid = parse_ref(ref)
        rows = self._rows_for_session(db, sid)
        objs, bad = [], []
        for i, text in enumerate(rows, 1):
            res = parse_jsonl(text)
            objs.extend(res.objs)
            if res.bad_lines:
                bad.append(i)
        # bad_items 与 bad_lines 同为「第几条事件」：本后端没有"文件行号"这一层，
        # 显式给出以免引擎回退到 bad_files 而让尺度契约含糊（显式给出才不含糊）。
        return ParseResult(objs=objs, bad_lines=bad, bad_items=list(bad))

    def read_raw(self, ref: str) -> str:
        """读出「每行一条 JSON」的等价文本，**仅供引擎的行数 / 解析 / 残留校验复用**。

        ⚠️ 这是**读侧**：只在内存里比较，从不 `encode`、从不落盘 ⇒ 含孤立代理也不会崩，
        故**有意**不用 `dumps_safe`（那是**写出点**才需要的安全阀）。
        **不要**把本方法的返回值直接写盘——要写盘请改走 `dumps_safe`。
        """
        lines = self.read_lines(ref)
        return "\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + ("\n" if lines else "")

    def branch_target(self, src_ref: str, new_id: str) -> str:
        """产物与源同库、不同 session_id —— 不是新文件。"""
        db, _ = parse_ref(src_ref)
        dst_ref = make_ref(db, new_id)
        self._src_of_dst[dst_ref] = src_ref
        return dst_ref

    def write_branch(self, tmp_ref: str, lines: list[dict]) -> None:
        """只暂存；真正的库写入推迟到 finalize（= 引擎的"发布"动作）。

        这与 JSONL 后端写 .tmp 是同一种语义：校验期内容**对外不可见**，
        但校验器能读到。区别只是介质不同。
        """
        self._pending[tmp_ref] = [dict(o) for o in lines]

    def finalize_branch(self, tmp_ref: str, dst_ref: str) -> None:
        """一次性把 7 张表写进同一个事务（原子；任一步失败则整体回滚）。"""
        lines = self._pending.pop(tmp_ref, None)
        if lines is None:
            raise RuntimeError(f"finalize 找不到待落位内容：{tmp_ref}")
        src_ref = self._src_of_dst.pop(dst_ref, None)
        if not src_ref:
            raise RuntimeError(f"finalize 缺少源会话引用（branch_target 未被调用？）：{dst_ref}")
        db, new_id = parse_ref(dst_ref)
        _, src_id = parse_ref(src_ref)

        cut = len(lines)
        new_key = f"agent:{self.AGENT_ID}:{BRANCH_KEY_PREFIX}{new_id[:8]}"
        now_ms = int(time.time() * 1000)
        con = self._connect()
        try:
            con.execute("BEGIN")
            self._copy_session_rows(con, src_id, new_id, new_key, lines, cut, now_ms)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        # 分支的 header.id 已由引擎的 rewrite_ids 换过，库内 header 与之一致

    def _copy_session_rows(self, con, src_id, new_id, new_key, lines, cut, now_ms) -> None:
        cur = con.cursor()

        def dup(table, overrides, extra=None):
            cs = self._cols(cur, table)
            sql = f"SELECT * FROM {table} WHERE session_id=?"
            if extra:
                sql += " AND " + extra
            n = 0
            for r in cur.execute(sql, (src_id,)).fetchall():
                d = {k: r[k] for k in cs}
                d.update(overrides)
                cur.execute(
                    f"INSERT INTO {table} ({','.join(cs)}) VALUES ({','.join('?' * len(cs))})",
                    [d[k] for k in cs],
                )
                n += 1
            return n

        # 1. 会话窗口
        dup("session_windows", {
            "session_id": new_id, "session_key": new_key,
            "previous_session_id": src_id, "reason": "fork",
            "created_at": now_ms, "updated_at": now_ms,
            "transcript_updated_at": now_ms, "transcript_observed_at": now_ms,
        })
        # 2. 转录本体（引擎已截断 + 换 header.id，这里按 seq 顺序落库）
        #    分支一律**写明文 event_json**（不压缩）：event_zstd 留 NULL 即满足产品的
        #    "二者恰有其一" CHECK；若该代 schema 有 event_utf8_bytes 列，按产品口径
        #    补上原文长度（读侧也用它做长度校验，写对了才自洽）。
        te_cs = self._cols(cur, "transcript_events")
        has_utf8_col = "event_utf8_bytes" in te_cs
        for seq, obj in enumerate(lines):
            text = dumps_safe(obj)
            if has_utf8_col:
                cur.execute(
                    "INSERT INTO transcript_events "
                    "(session_id,seq,event_json,created_at,event_utf8_bytes) VALUES (?,?,?,?,?)",
                    (new_id, seq, text, now_ms, len(text.encode("utf-8"))),
                )
            else:
                cur.execute(
                    "INSERT INTO transcript_events (session_id,seq,event_json,created_at) VALUES (?,?,?,?)",
                    (new_id, seq, text, now_ms),
                )
        # 3/4. 反规范化索引 + 投影行：随事件裁剪
        dup("transcript_event_identities", {"session_id": new_id}, extra=f"seq < {cut}")
        dup("session_transcript_active_events", {"session_id": new_id}, extra=f"event_seq < {cut}")
        # 5. 代际水印
        dup("transcript_rewrite_watermarks",
            {"session_id": new_id, "generation": uuid.uuid4().hex, "updated_at": now_ms})
        # 6. 投影进度（漏了它 → "projection is rebuilding"）
        msg_cnt = cur.execute(
            "SELECT COUNT(*) FROM session_transcript_active_events "
            "WHERE session_id=? AND message_position IS NOT NULL", (new_id,)).fetchone()[0]
        ev_cnt = cur.execute(
            "SELECT COUNT(*) FROM session_transcript_active_events WHERE session_id=?",
            (new_id,)).fetchone()[0]
        cur.execute(
            "INSERT INTO session_transcript_index_state "
            "(session_id,indexed_seq,leaf_event_id,needs_rebuild,"
            "active_event_count,active_message_count,updated_at) VALUES (?,?,?,0,?,?,?)",
            (new_id, cut - 1, lines[-1].get("id") if lines else None, ev_cnt, msg_cnt, now_ms),
        )
        # 7. 会话节点
        #    entry_valid 必须**自己写 1**（2026.11.3 无触发器；官方写入投影 bindSessionNode
        #    也内联写 1；读侧 `entry_valid !== 1` 直接判 "requires repair"，而 doctor 对
        #    干净行是 no-op）。写 0 会让分支必然读不出来。
        sn_cs = self._cols(cur, "session_nodes")
        src_node = cur.execute(
            "SELECT * FROM session_nodes WHERE current_session_id=?", (src_id,)).fetchone()
        if src_node:
            d = {k: src_node[k] for k in sn_cs}
            src_key = src_node["session_key"]
            entry = json.loads(d["entry_json"]) if d.get("entry_json") else {}
            entry.update({"sessionId": new_id, "updatedAt": now_ms, "sessionStartedAt": now_ms})
            # 谱系**必须同时进 entry**：2026.11.3 的 validateCanonicalSessionRowEntry 在
            # read / admission 两种模式下都校验三条等式（列 ←→ entry_json）：
            #   ① parent_session_key      == entry.parentSessionKey ?? entry.spawnedBy
            #   ② spawned_by              == entry.spawnedBy
            #   ③ fork_source_session_key == entry.forkSource?.sessionKey
            entry["parentSessionKey"] = src_key
            entry["forkSource"] = {"sessionKey": src_key, "sessionId": src_id}
            # ②：源 entry 带 spawnedBy 时必须原样复刻到列上；源没有该字段则列为 NULL
            # （源节点自己的 spawned_by 是「源被谁派生」，不能直接搬到分支上）。
            spawned_by = entry.get("spawnedBy")
            d.update({
                "session_key": new_key, "current_session_id": new_id,
                "entry_json": dumps_safe(entry),
                "entry_valid": 1, "updated_at": now_ms, "created_at": now_ms,
                "fork_source_session_key": src_key,
                "fork_source_session_id": src_id,
                "fork_source_entry_id": None,
                "parent_session_key": src_key,
                "spawned_by": spawned_by,
            })
            cur.execute(
                f"INSERT INTO session_nodes ({','.join(sn_cs)}) VALUES ({','.join('?' * len(sn_cs))})",
                [d[k] for k in sn_cs],
            )

    def backup_transcript(self, ref: str, backups_dir: str) -> str:
        """备份**整个数据库**（含 WAL）。用 SQLite 官方 backup API 保证一致性。"""
        db, _ = parse_ref(ref)
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

    # ------------------------------------------------------------------
    # D. 注册（OpenClaw 无 sessions.json；session_nodes 已在 finalize 写入）
    # ------------------------------------------------------------------
    def register_branch(self, src, new_id, dst_path, name, parent_id=None, at_seq=None,
                        prefix_fp=None) -> None:
        # 产品索引（session_nodes）随 finalize 落地；这里只写旁路谱系索引
        super(OpenClawJsonlAdapter, self).register_branch(
            src, new_id, dst_path, name, parent_id=parent_id, at_seq=at_seq,
            prefix_fp=prefix_fp)

    def unregister_branch(self, new_id: str) -> None:
        super(OpenClawJsonlAdapter, self).unregister_branch(new_id)
        try:
            con = self._connect()
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("DELETE FROM session_nodes WHERE current_session_id=?", (new_id,))
            con.execute("DELETE FROM session_windows WHERE session_id=?", (new_id,))
            con.commit()
            con.close()
        except Exception:
            pass

    def load_session_meta(self, session_id: str) -> SessionMeta | None:
        for ref, sid, _ in self._sessions():
            if sid == session_id:
                cwd = ""
                try:
                    lines = self.read_lines(ref)
                    if lines:
                        cwd = lines[0].get("cwd") or ""
                except Exception:
                    pass
                return SessionMeta(id=session_id, title="", status="", created_at=os.path.getmtime(self.DB_PATH), cwd=cwd)
        return None

    # ------------------------------------------------------------------
    # E. 体检
    # ------------------------------------------------------------------
    def storage_root(self) -> str:
        return self.DB_PATH

    def verify_storage(self) -> list[VerifyItem]:
        ok = os.path.exists(self.DB_PATH)
        n = ev = 0
        nodes = bad_nodes = None  # None = 没查成（表可能不存在）
        zstd_rows = None  # None = 没查成；list = 压缩事件行 (session_id, seq, blob, utf8_bytes)
        if ok:
            try:
                con = self._connect(readonly=True)
                n = con.execute("SELECT COUNT(DISTINCT session_id) FROM transcript_events").fetchone()[0]
                ev = con.execute("SELECT COUNT(*) FROM transcript_events").fetchone()[0]
                try:
                    nodes = con.execute("SELECT COUNT(*) FROM session_nodes").fetchone()[0]
                    bad_nodes = con.execute(
                        "SELECT COUNT(*) FROM session_nodes WHERE entry_valid <> 1").fetchone()[0]
                except Exception:
                    nodes = bad_nodes = None
                try:
                    tc = [d[0] for d in con.execute(
                        "SELECT * FROM transcript_events LIMIT 0").description]
                    if "event_zstd" in tc:
                        sel = ["session_id", "seq", "event_zstd"]
                        if "event_utf8_bytes" in tc:
                            sel.append("event_utf8_bytes")
                        zstd_rows = con.execute(
                            f"SELECT {','.join(sel)} FROM transcript_events WHERE event_json IS NULL"
                        ).fetchall()
                        zstd_rows = [
                            (r[0], r[1], r[2], r[3] if len(sel) > 3 else None) for r in zstd_rows
                        ]
                    else:
                        # ≤2026.9.x 世代：表上没有 event_zstd 列 ⇒ 结构上不存在压缩事件
                        zstd_rows = []
                except Exception:
                    zstd_rows = None
                con.close()
            except Exception:
                pass
        # entry_valid：真读库统计（旧实现恒 True，是假阳性）
        if nodes is None:
            valid_ok, valid_note = False, "库中无 session_nodes 表（无法校验）"
        else:
            valid_ok = bad_nodes == 0
            valid_note = (
                f"{nodes} 个节点全部 entry_valid=1（读取侧要求 ==1，否则报 requires repair）"
                if valid_ok else
                f"{bad_nodes}/{nodes} 个节点 entry_valid≠1 ⇒ OpenClaw 会报 "
                "`invalid persisted session row requires repair`；本适配器新写的分支恒为 1"
            )
        # 压缩事件：现在有解码能力 ⇒ **真解一遍**，解不了才判红（判红会拉起 --verify 退出码）
        if zstd_rows is None:
            zstd_ok, zstd_note = False, "读不到 transcript_events 的压缩列（无法校验）"
        elif not zstd_rows:
            zstd_ok = True
            zstd_note = (
                "无压缩事件（事件表没有 event_zstd 列，或 event_json 全部有值）"
            )
        else:
            impl_name, dec = zstd_support()
            if dec is None:
                zstd_ok = False
                zstd_note = (
                    f"{len(zstd_rows)} 条事件为 zstd 压缩（event_json IS NULL, event_zstd 有值），"
                    f"但当前环境没有 zstd 解码实现 ⇒ 读这些会话会直接报错。修复：{ZSTD_FIX_ADVICE}"
                )
            else:
                broken = None
                for sid, seq, blob, utf8_bytes in zstd_rows:
                    try:
                        self._decode_zstd_event(blob, utf8_bytes, sid, seq)
                    except ForkError as e:
                        broken = str(e)
                        break
                zstd_ok = broken is None
                zstd_note = (
                    f"{len(zstd_rows)} 条 zstd 压缩事件已全部解码且长度校验通过"
                    f"（{impl_name}；读侧等价 coalesce(event_json, decode(event_zstd))）"
                    if zstd_ok else broken
                )
        return [
            VerifyItem(
                "OpenClaw SQLite 转录库",
                "L2" if ev else "L1",
                ok and ev > 0,
                f"{self.DB_PATH}（{n} 个会话 / {ev} 条事件）" if ok
                else f"缺失：{self.DB_PATH}（先跑 `openclaw agent` 产生会话）",
            ),
            VerifyItem(
                "行存储契约 (session_id, seq) → event_json",
                "L2" if ev else "L1",
                ev > 0,
                "与 JSONL 的「行序 → 条目」同构；引擎按 seq 排序读取",
            ),
            VerifyItem("会话节点 entry_valid", "L1", valid_ok, valid_note),
            VerifyItem("压缩事件 (event_zstd)", "L1", zstd_ok, zstd_note),
        ]

    # ------------------------------------------------------------------
    # G. 文案
    # ------------------------------------------------------------------
    def activation_hint(self) -> str:
        return (
            "OpenClaw 分支已写入 %s（7 张表，事务提交，entry_valid=1）：\n"
            "            ① `openclaw sessions --json` 即可列出；用 "
            "`openclaw agent --session-key agent:%s:%s<8位>` 续跑\n"
            "            ② （可选）跑一次 `openclaw doctor` 做官方自检 —— entry_valid 已由"
            "本适配器直接写 1，doctor 不是必需步骤"
            % (os.path.basename(self.DB_PATH), self.AGENT_ID, BRANCH_KEY_PREFIX)
        )

    def produce_hint(self) -> str:
        return (
            "跑一次 OpenClaw（如 `openclaw agent --local --session-key agent:%s:demo "
            "--message 'hi'`），转录会写入 %s" % (self.AGENT_ID, self.DB_PATH)
        )

    def publish_hint(self) -> str:
        return "openclaw doctor --fix"

    def backup_note(self) -> str:
        return "整个 SQLite 数据库（官方 backup API，一致性快照）"

    def readonly_hint(self, dst_ref: str) -> str:
        # 分支是库里的行，不是文件 —— 没有可改权限位的对象
        return ""

"""OpenClaw 2026.11.3 契约测试：entry_valid / 三条件血缘等式 / zstd 压缩事件的"打分支不解码"。

三条都是"不改就真的坏"的兼容点（证据来自产品源码与二进制，不是猜测）：

  1. `session_nodes.entry_valid` 必须写 **1**。2026.11.3 已无 legacy 触发器，官方写入
     投影 `bindSessionNode` 内联写 1；读取侧 `entry_valid !== 1` 直接抛
     `invalid persisted session row requires repair`，而 `doctor` 对干净行是 no-op
     （不会把 0 改成 1）⇒ 写 0 的分支必然读不出来。
  2. `validateCanonicalSessionRowEntry` 的三条等式（列 ←→ entry_json）：
       parent_session_key      == entry.parentSessionKey ?? entry.spawnedBy
       spawned_by              == entry.spawnedBy
       fork_source_session_key == entry.forkSource?.sessionKey
     本文件覆盖"源 entry 有 spawnedBy"与"没有"两种情形。
  3. ≥1024B 的事件默认落成 `event_json = NULL, event_zstd = <zstd>`，原文长度记在
     `event_utf8_bytes`，派生投影列（`message_role` / `navigation_type` / …）在压缩**之前**
     就算好落库。用户口径（2026-11-16）：**打分支 = 定位 + 复制**，复制是原样搬字节，
     所以打分支**不需要解码**；解码只服务于"要读正文"（--match / 预览 / 体检真解一遍）。
     本文件因此固化两组相反方向的断言：
       · 有解码能力时：压缩源 fork 出的分支**仍保持压缩形态**、与源逐字节一致；
       · **没有任何解码实现时**：`--line` 路径**仍然成功**（这是本次改动的核心断言），
         需要正文的路径**明确降级**（占位正文 + note）而不是崩。

与 test_openclaw_sqlite_adapter.py 的分工：那个文件固化"写入 7 张表"的主流程，
本文件固化上面三条 2026.11.3 特有契约。

压缩能力本身也要求零依赖：本文件用 stdlib `compression.zstd`（Py3.14+）或系统
`zstd` 命令造夹具，**不 import zstandard**（pyproject 已无该依赖）。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core import adapter_openclaw_sqlite as ocs  # noqa: E402
from fork_core.adapter_openclaw_sqlite import (  # noqa: E402
    UNDECODED_KEY, UNDECODED_TEXT, OpenClawSqliteAdapter, make_ref, zstd_support,
)
from fork_core.engine import ForkError, create_fork  # noqa: E402

SRC_ID = "bbbbbbbb-1111-2222-3333-444444444444"
SRC_KEY = "agent:main:lab-with-spawn"
NOW = 1800000000000

# 2026.11.3 的 DDL 最小子集（7 张表 + 压缩三列 + 派生投影列 + entry_valid CHECK）。
# 投影列按产品真实 schema 建（navigation_json / navigation_* / message_role）：
# 它们**在压缩行上也有值**，正是"不需要解码就能定位"的依据。
# 注意：**没有** session_nodes 的 legacy 触发器 —— 那正是 2026.11.3 的事实。
DDL = """
CREATE TABLE session_windows (
  session_id TEXT NOT NULL PRIMARY KEY, session_key TEXT NOT NULL,
  previous_session_id TEXT, reason TEXT, session_scope TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  transcript_updated_at INTEGER, transcript_observed_at INTEGER, status TEXT
) STRICT;
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT,
  created_at INTEGER NOT NULL, event_zstd BLOB, event_utf8_bytes INTEGER,
  navigation_json TEXT,
  navigation_type TEXT, navigation_custom_type TEXT,
  navigation_display INTEGER NOT NULL DEFAULT 0, message_role TEXT,
  navigation_last_type TEXT, navigation_last_custom_type TEXT,
  navigation_valid INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (session_id, seq),
  CHECK ((event_json IS NOT NULL AND event_zstd IS NULL)
      OR (event_json IS NULL AND event_zstd IS NOT NULL))
) STRICT;
CREATE TABLE transcript_event_identities (
  session_id TEXT NOT NULL, event_id TEXT NOT NULL, seq INTEGER NOT NULL,
  event_type TEXT NOT NULL, parent_id TEXT, message_idempotency_key TEXT,
  created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE session_transcript_active_events (
  session_id TEXT NOT NULL, active_position INTEGER NOT NULL, event_seq INTEGER NOT NULL,
  message_position INTEGER, context_eligible INTEGER,
  PRIMARY KEY (session_id, active_position)
) STRICT;
CREATE TABLE transcript_rewrite_watermarks (
  session_id TEXT NOT NULL, generation TEXT NOT NULL, updated_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, generation)
) STRICT;
CREATE TABLE session_transcript_index_state (
  session_id TEXT NOT NULL PRIMARY KEY, indexed_seq INTEGER NOT NULL, leaf_event_id TEXT,
  needs_rebuild INTEGER NOT NULL DEFAULT 0, active_event_count INTEGER NOT NULL DEFAULT 0,
  active_message_count INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
) STRICT;
CREATE TABLE session_nodes (
  session_key TEXT NOT NULL PRIMARY KEY, current_session_id TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  entry_valid INTEGER NOT NULL DEFAULT 0 CHECK (entry_valid IN (-1, 0, 1)),
  updated_at INTEGER NOT NULL, created_at INTEGER,
  fork_source_session_key TEXT, fork_source_session_id TEXT, fork_source_entry_id TEXT,
  parent_session_key TEXT, spawned_by TEXT
) STRICT;
"""

EVENTS = [
    {"type": "session", "version": 3, "id": SRC_ID, "timestamp": "2026-11-16T06:00:00.000Z",
     "cwd": "/tmp/ws"},
    {"type": "message", "id": "m001", "parentId": None,
     "message": {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: V3-OK"}]}},
    {"type": "message", "id": "m002", "parentId": "m001",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "V3-OK"}]}},
]

# "--match 降级"用的一组：明文 assistant 在 seq2，压缩 assistant 在 seq4。
# 这样"命中明文事件"与"正文只在压缩事件里"两种情形都能构造出来。
EVENTS_MATCH = [
    EVENTS[0],
    EVENTS[1],
    EVENTS[2],
    {"type": "message", "id": "m003", "parentId": "m002",
     "message": {"role": "user", "content": [{"type": "text", "text": "再答一次"}]}},
    {"type": "message", "id": "m004", "parentId": "m003",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "COMPRESSED-ONLY-OK"}]}},
]


# ---------------------------------------------------------------------------
# 零依赖压缩：stdlib（Py3.14+）→ 系统 `zstd` 命令 → 不可用
# ---------------------------------------------------------------------------
def _compress_raw(raw: bytes) -> bytes:
    try:
        from compression import zstd as _z  # Py3.14+

        return _z.compress(raw)
    except Exception:
        pass
    exe = shutil.which("zstd")
    if not exe:
        raise RuntimeError("本机既无 stdlib compression.zstd 也无 zstd 命令，无法构造压缩夹具")
    proc = subprocess.run([exe, "-q", "-c"], input=raw, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout


def _decompress_raw(blob: bytes) -> bytes:
    """仅用于"内容是原文"这类断言；走适配器自己的探测链（不引第三方包）。"""
    _, dec = zstd_support()
    assert dec is not None, "本机没有 zstd 解码实现：这条断言需要解码能力（系统 zstd 或 Py3.14+）"
    return dec(blob, ocs._ZSTD_MAX_OUTPUT)


def _decompress_cli(blob: bytes) -> bytes:
    """直接从系统 `zstd` 命令解（**不经**适配器的探测链，供假 stdlib 模块回调用）。"""
    exe = shutil.which("zstd")
    assert exe, "本机没有 zstd 命令"
    proc = subprocess.run([exe, "-d", "-q", "-c"], input=blob, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout


try:
    _compress_raw(b"probe")
    HAS_COMPRESSOR = True
except Exception as _e:  # pragma: no cover - 只影响本机能跑多少断言
    HAS_COMPRESSOR = False
    print(f"⚠️ 跳过压缩相关断言：{_e}")
HAS_DECODER = zstd_support()[1] is not None


def zbytes(obj) -> tuple[bytes, int]:
    """按产品口径压一条事件：返回 (zstd 字节, 原文 UTF-8 字节数)。"""
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return _compress_raw(raw), len(raw)


def _predicate_cols(obj) -> dict:
    """模拟产品的派生投影（deriveTranscriptPredicateFields 的取值面）。"""
    etype = obj.get("type")
    role = (obj.get("message") or {}).get("role")
    return {
        "navigation_type": etype if etype in ("session", "message", "reset", "compaction",
                                             "custom_message", "custom") else None,
        "navigation_display": 0,
        "message_role": role if role in ("user", "assistant", "toolResult", "system") else None,
        "navigation_last_type": etype,
        "navigation_valid": 1,
    }


def build_db(root, src_entry, *, events=EVENTS, compress_seqs=(), bad_len_seq=None,
             corrupt_seq=None):
    """造一个 2026.11.3 形状的最小库；返回 (db_path, adapter)。

    compress_seqs：把这几条 seq 的事件改成产品的压缩形态
        `event_json = NULL, event_zstd = <真 zstd 字节>, event_utf8_bytes = 原文长度`，
        并像产品那样同时写入 `navigation_json` 与派生投影列（**压缩行的投影列照样有值**）。
    bad_len_seq：把该 seq 的 event_utf8_bytes 故意写错（+1）——验证"解出来是半截"会被抓。
    corrupt_seq：把该 seq 的 zstd 字节截断 —— 验证解压失败会带 session_id + seq。
    """
    agent_dir = os.path.join(root, "state", "agents", "main")
    os.makedirs(os.path.join(agent_dir, "agent"), exist_ok=True)
    db = os.path.join(agent_dir, "agent", "openclaw-agent.sqlite")
    con = sqlite3.connect(db)
    con.executescript(DDL)
    for seq, o in enumerate(events):
        pred = _predicate_cols(o)
        if seq in compress_seqs:
            blob, nbytes = zbytes(o)
            if seq == corrupt_seq:
                blob = blob[: max(1, len(blob) // 2)]  # 截断 = 解压必失败
            if seq == bad_len_seq:
                nbytes += 1
            nav = json.dumps({"version": 1, "navigation": {"type": o.get("type"), "id": o["id"]}},
                             ensure_ascii=False)
            con.execute(
                "INSERT INTO transcript_events (session_id,seq,event_json,created_at,event_zstd,"
                "event_utf8_bytes,navigation_json,navigation_type,navigation_display,message_role,"
                "navigation_last_type,navigation_valid) VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?)",
                (SRC_ID, seq, NOW, blob, nbytes, nav, pred["navigation_type"],
                 pred["navigation_display"], pred["message_role"],
                 pred["navigation_last_type"], pred["navigation_valid"]))
        else:
            text = json.dumps(o, ensure_ascii=False)
            con.execute(
                "INSERT INTO transcript_events (session_id,seq,event_json,created_at,"
                "event_utf8_bytes,navigation_type,navigation_display,message_role,"
                "navigation_last_type,navigation_valid) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (SRC_ID, seq, text, NOW, len(text.encode("utf-8")), pred["navigation_type"],
                 pred["navigation_display"], pred["message_role"],
                 pred["navigation_last_type"], pred["navigation_valid"]))
        con.execute("INSERT INTO transcript_event_identities VALUES (?,?,?,?,?,?,?)",
                    (SRC_ID, o["id"], seq, o["type"], o.get("parentId"), None, NOW))
        if seq > 0:
            mp = seq - 1 if o["type"] == "message" else None
            con.execute("INSERT INTO session_transcript_active_events VALUES (?,?,?,?,?)",
                        (SRC_ID, seq - 1, seq, mp, 1))
    con.execute("INSERT INTO session_windows VALUES (?,?,?,?,?,?,?,?,?,?)",
                (SRC_ID, SRC_KEY, None, None, "conversation", NOW, NOW, NOW, NOW, None))
    con.execute("INSERT INTO transcript_rewrite_watermarks VALUES (?,?,?)", (SRC_ID, "gen1", NOW))
    con.execute("INSERT INTO session_transcript_index_state VALUES (?,?,?,0,?,?,?)",
                (SRC_ID, len(events) - 1, events[-1]["id"], len(events) - 1,
                 sum(1 for o in events[1:] if o["type"] == "message"), NOW))
    con.execute(
        "INSERT INTO session_nodes (session_key,current_session_id,entry_json,entry_valid,"
        "updated_at,created_at,parent_session_key,spawned_by) VALUES (?,?,?,1,?,?,?,?)",
        (SRC_KEY, SRC_ID, json.dumps(src_entry), NOW, NOW,
         src_entry.get("parentSessionKey"), src_entry.get("spawnedBy")))
    con.commit()
    con.close()
    os.environ["OPENCLAW_STATE_DIR"] = os.path.join(root, "state")
    return db, OpenClawSqliteAdapter()


def src_rows(db, sid):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM transcript_events WHERE session_id=? ORDER BY seq",
                           (sid,)).fetchall()
    finally:
        con.close()


def check_equalities(node, src_key):
    """三条等式（read / admission 两种模式都生效，形状必须完全对齐）。"""
    entry = json.loads(node["entry_json"])
    assert node["parent_session_key"] == (entry.get("parentSessionKey") or entry.get("spawnedBy")), \
        "等式① parent_session_key == entry.parentSessionKey ?? entry.spawnedBy"
    assert node["spawned_by"] == entry.get("spawnedBy"), "等式② spawned_by == entry.spawnedBy"
    assert node["fork_source_session_key"] == (entry.get("forkSource") or {}).get("sessionKey"), \
        "等式③ fork_source_session_key == entry.forkSource.sessionKey"
    assert node["entry_valid"] == 1, "entry_valid 必须写 1（读侧严格 ===1）"
    assert node["parent_session_key"] == src_key
    assert node["fork_source_session_key"] == src_key
    assert (entry.get("forkSource") or {}).get("sessionId") == SRC_ID
    return entry


class _NoDecode:
    """把"本机没有任何 zstd 解码实现"这件事隔离进来（不改环境、不卸任何东西）。"""

    def __enter__(self):
        self._saved = ocs._ZSTD_CACHE
        ocs._ZSTD_CACHE = (None, None)
        return self

    def __exit__(self, *exc):
        ocs._ZSTD_CACHE = self._saved
        return False


# ---------------------------------------------------------------------------
# 1. 源 entry 带 spawnedBy：列必须复刻同一个值（否则等式②不成立）
# ---------------------------------------------------------------------------
root1 = tempfile.mkdtemp(prefix="oc-contract-spawn-")
db1, ad1 = build_db(root1, {
    "sessionId": SRC_ID, "updatedAt": NOW,
    "parentSessionKey": "agent:main:root",
    "spawnedBy": "agent:main:root",   # 源是被 spawn 出来的子会话
})
r1 = create_fork(adapter=ad1, session_ref=SRC_ID, name="分支·带 spawnedBy",
                 backups_dir=os.path.join(root1, "backups"))
assert r1.ok and r1.verified, "全链路 fork 失败"

con = sqlite3.connect(db1)
con.row_factory = sqlite3.Row
node = con.execute("SELECT * FROM session_nodes WHERE current_session_id=?", (r1.new_id,)).fetchone()
assert node is not None, "分支节点未写入"
entry = check_equalities(node, SRC_KEY)
assert node["spawned_by"] == "agent:main:root", "源 entry 的 spawnedBy 必须原样复刻到列上"
assert entry.get("spawnedBy") == "agent:main:root", "entry 里的 spawnedBy 应随 entry 复制保留"
print("✓ 源 entry 带 spawnedBy：entry_valid=1 + 三条等式成立（spawned_by 已复刻）")

# 待校验队列不能有行（进队列会挡住续跑）——本适配器不写该表
assert not con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='session_canonical_validation_pending'"
).fetchone() or con.execute(
    "SELECT COUNT(*) FROM session_canonical_validation_pending").fetchone()[0] == 0
print("✓ 不往 session_canonical_validation_pending 写行（不挡续跑）")

# verify_storage 的 entry_valid 项必须真读库：把分支行改成 0 → 必须判失败
con.execute("UPDATE session_nodes SET entry_valid=0 WHERE current_session_id=?", (r1.new_id,))
con.commit()
items = {i.name: i for i in ad1.verify_storage()}
assert items["会话节点 entry_valid"].ok is False, "库里有 entry_valid≠1 时体检不能报绿（假阳性）"
assert "entry_valid≠1" in items["会话节点 entry_valid"].detail, items["会话节点 entry_valid"].detail
con.execute("UPDATE session_nodes SET entry_valid=1 WHERE current_session_id=?", (r1.new_id,))
con.commit()
assert {i.name: i for i in ad1.verify_storage()}["会话节点 entry_valid"].ok is True
print("✓ verify_storage：entry_valid 真读库（≠1 的行数如实报红，不再恒 True）")
con.close()

# ---------------------------------------------------------------------------
# 2. 源 entry 无 spawnedBy：列必须保持 NULL（不能搬源节点的 spawned_by）
# ---------------------------------------------------------------------------
root2 = tempfile.mkdtemp(prefix="oc-contract-nospawn-")
db2, ad2 = build_db(root2, {"sessionId": SRC_ID, "updatedAt": NOW})
r2 = create_fork(adapter=ad2, session_ref=SRC_ID, name="分支·无 spawnedBy",
                 backups_dir=os.path.join(root2, "backups"))
assert r2.ok and r2.verified, "全链路 fork 失败（无 spawnedBy 情形）"
con = sqlite3.connect(db2)
con.row_factory = sqlite3.Row
node = con.execute("SELECT * FROM session_nodes WHERE current_session_id=?", (r2.new_id,)).fetchone()
check_equalities(node, SRC_KEY)
assert node["spawned_by"] is None, "源 entry 无 spawnedBy ⇒ spawned_by 必须 NULL"
assert json.loads(node["entry_json"]).get("spawnedBy") is None
con.close()
print("✓ 源 entry 无 spawnedBy：三条等式仍成立（spawned_by 保持 NULL）")

if HAS_COMPRESSOR:
    # -----------------------------------------------------------------------
    # 3. 压缩事件：打分支**不需要解码**（复制原样搬字节），要正文才解码
    # -----------------------------------------------------------------------
    root3 = tempfile.mkdtemp(prefix="oc-contract-zstd-")
    db3, ad3 = build_db(root3, {"sessionId": SRC_ID, "updatedAt": NOW}, compress_seqs=(1, 2))
    ref3 = make_ref(db3, SRC_ID)

    # 3.0 先确认夹具真的是"产品的压缩形态"（否则后面的通过是假阳性）
    stored = src_rows(db3, SRC_ID)
    assert stored[0]["event_json"] is not None, "seq=0 应保持明文（header 永不被压缩）"
    for seq in (1, 2):
        r = stored[seq]
        assert r["event_json"] is None and isinstance(r["event_zstd"], bytes), \
            f"seq={seq} 应为 event_json=NULL + event_zstd=<blob>"
        assert r["event_utf8_bytes"] == len(json.dumps(EVENTS[seq], ensure_ascii=False).encode("utf-8"))
        # 关键证据面：压缩行的派生投影列**照样有值**（产品在压缩前就算好落库）
        assert r["message_role"] == EVENTS[seq]["message"]["role"], \
            f"seq={seq} 的 message_role 必须是产品在压缩前算好的值（这是'定位不用解码'的依据）"
        assert r["navigation_type"] == "message" and r["navigation_valid"] == 1
        assert r["navigation_json"], f"seq={seq} 的 navigation_json 必须非空（产品 CHECK 要求）"
    print("✓ 夹具是产品压缩形态（event_json=NULL / event_zstd=<zstd> / 派生投影列仍有值）")

    if HAS_DECODER:
        # 3.1 解码能力可用时：读出来的必须是原文（长度与内容都对）
        lines3 = ad3.read_lines(ref3)
        assert lines3 == EVENTS, "read_lines 解出的条目必须与压缩前的原文逐字段一致"
        checked3 = ad3.read_lines_checked(ref3)
        assert checked3.objs == EVENTS and not checked3.bad_lines, "压缩事件不该被算坏行"
        print("✓ 有解码能力：read_lines / read_lines_checked 读出与压缩前完全一致的原文")

    # 3.2 【核心】压缩源 → fork（--line）→ 分支仍是**压缩形态**，且与源逐字节一致
    r3 = create_fork(adapter=ad3, session_ref=SRC_ID, line_no=3, name="分支·压缩源",
                     backups_dir=os.path.join(root3, "backups"))
    assert r3.ok and r3.verified, "含压缩事件的会话应能正常 fork"
    bs = src_rows(db3, r3.new_id)
    assert len(bs) == 3, f"分支事件数应为 3，实际 {len(bs)}"
    # 首行 header：必须是明文、且 id 已换（header 从不被压缩，改它不需要解码）
    assert bs[0]["event_json"] is not None and bs[0]["event_zstd"] is None, \
        "header 行必须是明文（产品侧 header 永不被压缩）"
    assert json.loads(bs[0]["event_json"])["id"] == r3.new_id, "header.id 必须换成新会话 id"
    assert bs[0]["event_utf8_bytes"] == len(bs[0]["event_json"].encode("utf-8")), \
        "header 改写后 event_utf8_bytes 必须按新明文重算"
    # 其余行：压缩形态原样保留 + 逐字节一致 + 派生投影列也原样
    for seq in (1, 2):
        b, s = bs[seq], stored[seq]
        assert b["event_json"] is None, f"seq={seq} 分支行必须仍是 event_json=NULL（原样搬）"
        assert bytes(b["event_zstd"]) == bytes(s["event_zstd"]), \
            f"seq={seq} 的 event_zstd 字节必须与源**逐字节一致**"
        assert b["event_utf8_bytes"] == s["event_utf8_bytes"], \
            f"seq={seq} 的 event_utf8_bytes 必须原样（无改写就不动它）"
        assert b["navigation_json"] == s["navigation_json"], "navigation_json 必须原样"
        assert b["message_role"] == s["message_role"] and b["navigation_type"] == s["navigation_type"], \
            "派生投影列必须原样（否则产品的定位查询会看不到这条事件）"
        if HAS_DECODER:
            assert json.loads(_decompress_raw(bytes(b["event_zstd"]))) == EVENTS[seq], \
                f"seq={seq} 解出来必须与源事件完全一致"
    # 且源会话没被改动
    assert [x["event_json"] for x in src_rows(db3, SRC_ID)] == [x["event_json"] for x in stored]
    print("✓【核心】压缩源 fork：分支行仍是压缩形态（event_json NULL / event_zstd 原样 / "
          "event_utf8_bytes 原样），内容与源逐字节一致；仅 header 行改 id 并写明文")

    # 3.3 体检：有解码能力 ⇒ 压缩事件项 ok 且带统计
    zok = {i.name: i for i in ad3.verify_storage()}["压缩事件 (event_zstd)"]
    if HAS_DECODER:
        assert zok.ok is True, f"能解码时体检不该判红：{zok.detail}"
        assert "条" in zok.detail and "解码" in zok.detail, zok.detail
        print("✓ verify_storage：压缩事件已全部解码 ⇒ 报 ok（%s）" % zok.detail[:34])
    else:
        assert zok.ok is True and zok.review is True, zok.detail
        print("✓ verify_storage：无解码实现 ⇒ 该项 ok+review（不拉 --verify 退出码）")

    if HAS_DECODER:
        # 3.4 event_utf8_bytes 不符（多 1 字节）⇒ 必须报错，不能把半截当原文
        root4 = tempfile.mkdtemp(prefix="oc-contract-badlen-")
        db4, ad4 = build_db(root4, {"sessionId": SRC_ID, "updatedAt": NOW},
                            compress_seqs=(2,), bad_len_seq=2)
        try:
            ad4.read_lines(make_ref(db4, SRC_ID))
            raise AssertionError("event_utf8_bytes 不符时必须抛 ForkError（防静默解出半截）")
        except ForkError as e:
            msg = str(e)
            assert "长度不符" in msg and "event_utf8_bytes" in msg, msg
            assert f"session_id={SRC_ID}" in msg and "seq=2" in msg, msg
        b4 = {i.name: i for i in ad4.verify_storage()}["压缩事件 (event_zstd)"]
        assert b4.ok is False and "长度不符" in b4.detail, b4.detail
        print("✓ event_utf8_bytes 不符 → ForkError（带 session_id + seq），体检同步判红")

        # 3.5 zstd 字节损坏 ⇒ 必须报解压失败，并带 session_id + seq
        root5 = tempfile.mkdtemp(prefix="oc-contract-corrupt-")
        db5, ad5 = build_db(root5, {"sessionId": SRC_ID, "updatedAt": NOW},
                            compress_seqs=(1,), corrupt_seq=1)
        try:
            ad5.read_lines(make_ref(db5, SRC_ID))
            raise AssertionError("zstd 字节损坏时必须抛 ForkError，不能跳过该行")
        except ForkError as e:
            msg = str(e)
            assert "解压失败" in msg and f"session_id={SRC_ID}" in msg and "seq=1" in msg, msg
        print("✓ event_zstd 字节损坏 → ForkError（含 session_id + seq，不静默跳过）")

    # -----------------------------------------------------------------------
    # 3.6 【本次的核心断言】**没有任何解码实现**时，--line 路径仍然成功
    # -----------------------------------------------------------------------
    root6 = tempfile.mkdtemp(prefix="oc-contract-nodecode-")
    db6, ad6 = build_db(root6, {"sessionId": SRC_ID, "updatedAt": NOW}, compress_seqs=(1, 2))
    with _NoDecode():
        assert zstd_support() == (None, None), "隔离后应探测不到任何解码实现"
        # (a) 读取降级为元数据视图（不抛、不崩）
        view = ad6.read_lines(make_ref(db6, SRC_ID))
        assert len(view) == 3, len(view)
        ph = view[2]
        assert ph.get(UNDECODED_KEY) is not None, "压缩行应给出元数据占位视图"
        assert ph["type"] == "message" and ph["message"]["role"] == "assistant", \
            "占位视图的类型/角色必须来自派生投影列（这是'不解码也能定位'的证据）"
        assert ph["id"] == "m002", "占位视图的 id 必须来自 transcript_event_identities"
        assert ad6.get_text(ph) == UNDECODED_TEXT, "正文必须给**明确占位**（不是空串）"
        assert any("zstd" in n for n in ad6.notes), "降级必须留下 note（不许静默）"
        # (b) --line 打分支：全程不需要解码
        r6 = create_fork(adapter=ad6, session_ref=SRC_ID, line_no=3, name="分支·无解码",
                         backups_dir=os.path.join(root6, "backups"))
        assert r6.ok and r6.verified, "没有任何 zstd 实现时 --line 也必须能打出分支"
        nb = src_rows(db6, r6.new_id)
        assert len(nb) == 3, len(nb)
        assert json.loads(nb[0]["event_json"])["id"] == r6.new_id, "header.id 仍必须正确改写"
        for seq in (1, 2):
            assert nb[seq]["event_json"] is None and nb[seq]["event_zstd"] is not None, \
                "无解码实现时分支行同样保持压缩形态（原样搬）"
            assert nb[seq]["event_utf8_bytes"] == stored[seq]["event_utf8_bytes"]
        assert any("原样字节复制" in n for n in ad6.notes), \
            "压缩行未参与内容层核对这件事必须说出来"

        # (c) 压缩字节里**确实出现**源 id（字面量）⇒ 这行必须改写 ⇒ 没有解码实现时
        #     必须**明确报错点名 seq**，不许静默放过（放过 = 分支留残留 id，体检变红）
        con = sqlite3.connect(db6)
        con.execute("UPDATE transcript_events SET event_zstd=? WHERE session_id=? AND seq=2",
                    (b"zstd-but-literal:" + SRC_ID.encode(), SRC_ID))
        con.commit()
        con.close()
        try:
            create_fork(adapter=ad6, session_ref=SRC_ID, line_no=3, name="分支·需改写",
                        backups_dir=os.path.join(root6, "backups"))
            raise AssertionError("压缩字节含源 id 且无解码实现时必须抛 ForkError")
        except ForkError as e:
            msg = str(e)
            assert "seq=2" in msg and SRC_ID in msg, msg
            assert "--line 2" in msg and "zstd" in msg, msg
            assert "结构性残留" in msg, msg
    print("✓【核心断言】无任何 zstd 解码实现 ⇒ --line 打分支**仍然成功**，"
          "分支行仍为压缩形态；读取降级为元数据视图 + 明确占位正文 + note；"
          "压缩字节含源 id 时明确报错点名 seq")

    # -----------------------------------------------------------------------
    # 4. 需要正文的路径：明确降级，不是"整条路崩"
    # -----------------------------------------------------------------------
    root7 = tempfile.mkdtemp(prefix="oc-contract-match-")
    db7, ad7 = build_db(root7, {"sessionId": SRC_ID, "updatedAt": NOW}, compress_seqs=(4,),
                        events=EVENTS_MATCH)
    with _NoDecode():
        # 4.1 --match 命中**明文** assistant ⇒ 照常能打分支（路径本身不崩）
        r7 = create_fork(adapter=ad7, session_ref=SRC_ID, match_text="V3-OK",
                         name="分支·无解码 match", backups_dir=os.path.join(root7, "backups"))
        assert r7.ok and r7.verified and r7.cut == 3, f"cut={r7.cut}（应切在明文 assistant 之后）"
        assert any("--match" in n and "zstd" in n for n in ad7.notes), \
            "--match 在降级模式下必须显式说明只搜了明文事件"
        # 4.2 正文只在压缩事件里 ⇒ 明确报"找不到"，而不是解码失败的内部错误
        ad7b = ad7
        ad7b._undecoded_noted.clear()
        ad7b.notes.clear()
        try:
            create_fork(adapter=ad7b, session_ref=SRC_ID, match_text="COMPRESSED-ONLY-OK",
                        name="分支·压缩正文", backups_dir=os.path.join(root7, "backups"))
            raise AssertionError("正文只在压缩事件里时应明确报'未找到'")
        except ForkError as e:
            msg = str(e)
            assert "match text not found" in msg, msg
        assert any("正文读不到" in n for n in ad7b.notes), "降级要留下可读的说明"
    print("✓ 无解码实现时：--match 命中明文事件照常可用（带降级说明）；"
          "正文只在压缩事件里 ⇒ 明确报未找到，不是解码内部异常")

    # -----------------------------------------------------------------------
    # 4.3 解码实现优先取 stdlib：注入一个转调系统 zstd 的假 stdlib 模块，
    #     验证"探测顺序"真的按 stdlib 先行（Py3.14+ 的真实路径）
    # -----------------------------------------------------------------------
    class _FakeStdlibZstd(types.ModuleType):
        def decompress(self, data, max_output_size=None):  # noqa: D401（stdlib 同名接口）
            return _decompress_cli(data)

    _saved = {k: sys.modules.get(k) for k in ("compression", "compression.zstd")}
    try:
        fake = types.ModuleType("compression")
        fake.zstd = _FakeStdlibZstd("compression.zstd")
        sys.modules["compression"] = fake
        sys.modules["compression.zstd"] = fake.zstd
        ocs._ZSTD_CACHE = None
        impl_name, dec = zstd_support()
        assert impl_name == "stdlib compression.zstd" and dec is not None, impl_name
        if HAS_DECODER:
            assert ad3.read_lines(ref3) == EVENTS, "stdlib 分支也应解出原文"
        print("✓ stdlib `compression.zstd` 可用时优先选它（探测链第一级）")
    finally:
        for k, v in _saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        ocs._ZSTD_CACHE = None

else:
    print("⚠️ 本机既无 stdlib `compression.zstd` 也无 `zstd` 命令："
          "跳过第 3/4 组（压缩形态与无解码降级）断言")

# ---------------------------------------------------------------------------
# 5. 旧 schema（无 event_zstd / event_utf8_bytes / 投影列）仍可读可 fork
# ---------------------------------------------------------------------------
root8 = tempfile.mkdtemp(prefix="oc-contract-legacy-")
db8, ad8 = build_db(root8, {"sessionId": SRC_ID, "updatedAt": NOW})
con = sqlite3.connect(db8)
con.executescript("""
DROP TABLE transcript_events;
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL,
  created_at INTEGER NOT NULL, PRIMARY KEY (session_id, seq)
) STRICT;
""")
for seq, o in enumerate(EVENTS):
    con.execute("INSERT INTO transcript_events VALUES (?,?,?,?)",
                (SRC_ID, seq, json.dumps(o, ensure_ascii=False), NOW))
con.commit()
con.close()
assert ad8.read_lines(make_ref(db8, SRC_ID)) == EVENTS, "旧 schema 应逐条读出原文"
lit = {i.name: i for i in ad8.verify_storage()}["压缩事件 (event_zstd)"]
assert lit.ok is True and lit.review is False, \
    f"没有 event_zstd 列的旧库不存在压缩事件，不该判红也不该提示复核：{lit.detail}"
r8 = create_fork(adapter=ad8, session_ref=SRC_ID, name="分支·旧 schema",
                 backups_dir=os.path.join(root8, "backups"))
assert r8.ok and r8.verified, "旧 schema（无 event_zstd / event_utf8_bytes / 投影列）应能 fork"
old_branch = src_rows(db8, r8.new_id)
assert [json.loads(x["event_json"])["id"] for x in old_branch[1:]] == [o["id"] for o in EVENTS[1:]]
print("✓ 旧 schema（2026.9.x，无 event_zstd / event_utf8_bytes / 投影列）仍可读可 fork")

print("\n✅ test_openclaw_sqlite_contract 全部通过"
      "（2026.11.3 三条契约 + 压缩行原样复制 + 无解码实现下仍可 --line 打分支 + 旧 schema 兼容）")

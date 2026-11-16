"""OpenClaw 2026.11.3 契约测试：entry_valid / 三条件血缘等式 / zstd 压缩事件真解码。

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
     `event_utf8_bytes`。本适配器**真解码**（stdlib `compression.zstd` 优先，退回
     `zstandard`），并用 `event_utf8_bytes` 校验长度 ⇒ 本文件用**真 zstd 压缩**造库，
     覆盖"解出来是原文 / 长度不符要报错 / 两级解码库都缺要带修复建议 / 坏字节要报
     session_id+seq"四条，以及"没有压缩列（2026.9.x）仍可读"。

与 test_openclaw_sqlite_adapter.py 的分工：那个文件固化"写入 7 张表"的主流程，
本文件固化上面三条 2026.11.3 特有契约。
"""
import json
import os
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zstandard  # noqa: E402  （pyproject 里的运行时依赖：压缩事件解码）

from fork_core import adapter_openclaw_sqlite as ocs  # noqa: E402
from fork_core.adapter_openclaw_sqlite import (  # noqa: E402
    OpenClawSqliteAdapter, make_ref, zstd_support,
)
from fork_core.engine import ForkError, create_fork  # noqa: E402

SRC_ID = "bbbbbbbb-1111-2222-3333-444444444444"
SRC_KEY = "agent:main:lab-with-spawn"
NOW = 1800000000000

# 2026.11.3 的 DDL 最小子集（7 张表 + event_zstd / event_utf8_bytes 列 + entry_valid CHECK）。
# 注意：**没有** session_nodes 的 legacy 触发器 —— 那正是 2026.11.3 的事实。
# event_utf8_bytes 在本夹具里建成**可空**列（真实库的 nullability 未取证）；写入侧两种
# 情形都能过 —— 有这列就带上原文长度，没有就不写（见 _copy_session_rows）。
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


def zbytes(obj) -> tuple[bytes, int]:
    """按产品口径压一条事件：返回 (zstd 字节, 原文 UTF-8 字节数)。"""
    text = json.dumps(obj, ensure_ascii=False)
    raw = text.encode("utf-8")
    return zstandard.ZstdCompressor().compress(raw), len(raw)


def build_db(root, src_entry, *, compress_seqs=(), bad_len_seq=None, corrupt_seq=None):
    """造一个 2026.11.3 形状的最小库；返回 (db_path, adapter)。

    compress_seqs：把这几条 seq 的事件改成产品的压缩形态
        `event_json = NULL, event_zstd = <真 zstd 字节>, event_utf8_bytes = 原文长度`
        （模拟 ≥1024B 事件被产品压缩落盘）。
    bad_len_seq：把该 seq 的 event_utf8_bytes 故意写错（±1）—— 用来验证"解出来是半截"会被抓。
    corrupt_seq：把该 seq 的 zstd 字节截断 —— 用来验证解压失败会带 session_id + seq。
    """
    agent_dir = os.path.join(root, "state", "agents", "main")
    os.makedirs(os.path.join(agent_dir, "agent"), exist_ok=True)
    db = os.path.join(agent_dir, "agent", "openclaw-agent.sqlite")
    con = sqlite3.connect(db)
    con.executescript(DDL)
    for seq, o in enumerate(EVENTS):
        if seq in compress_seqs:
            blob, nbytes = zbytes(o)
            if seq == corrupt_seq:
                blob = blob[: max(1, len(blob) // 2)]  # 截断 = 解压必失败
            if seq == bad_len_seq:
                nbytes += 1  # 长度不符（多 1 字节）
            con.execute(
                "INSERT INTO transcript_events "
                "(session_id,seq,event_json,created_at,event_zstd,event_utf8_bytes) "
                "VALUES (?,?,NULL,?,?,?)", (SRC_ID, seq, NOW, blob, nbytes))
        else:
            con.execute(
                "INSERT INTO transcript_events (session_id,seq,event_json,created_at) VALUES (?,?,?,?)",
                (SRC_ID, seq, json.dumps(o, ensure_ascii=False), NOW))
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
                (SRC_ID, len(EVENTS) - 1, EVENTS[-1]["id"], len(EVENTS) - 1, 2, NOW))
    con.execute(
        "INSERT INTO session_nodes (session_key,current_session_id,entry_json,entry_valid,"
        "updated_at,created_at,parent_session_key,spawned_by) VALUES (?,?,?,1,?,?,?,?)",
        (SRC_KEY, SRC_ID, json.dumps(src_entry), NOW, NOW,
         src_entry.get("parentSessionKey"), src_entry.get("spawnedBy")))
    con.commit()
    con.close()
    os.environ["OPENCLAW_STATE_DIR"] = os.path.join(root, "state")
    return db, OpenClawSqliteAdapter()


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

# ---------------------------------------------------------------------------
# 3. zstd 压缩事件：真解码（不是"检测到就报错"）—— 读侧等价 coalesce(json, decode)
# ---------------------------------------------------------------------------
root3 = tempfile.mkdtemp(prefix="oc-contract-zstd-")
db3, ad3 = build_db(root3, {"sessionId": SRC_ID, "updatedAt": NOW}, compress_seqs=(1, 2))
ref3 = make_ref(db3, SRC_ID)

# 3.0 先确认夹具真的是"产品的压缩形态"（否则后面的通过是假阳性）
con = sqlite3.connect(db3)
stored = con.execute(
    "SELECT seq,event_json,event_zstd,event_utf8_bytes FROM transcript_events ORDER BY seq").fetchall()
con.close()
assert stored[0][1] is not None, "seq=0 应保持明文"
for seq in (1, 2):
    r = stored[seq]
    assert r[1] is None and isinstance(r[2], bytes), f"seq={seq} 应为 event_json=NULL + event_zstd=<blob>"
    assert r[3] == len(json.dumps(EVENTS[seq], ensure_ascii=False).encode("utf-8")), \
        f"seq={seq} 的 event_utf8_bytes 应是原文 UTF-8 字节数"
print("✓ 夹具是产品压缩形态（event_json=NULL / event_zstd=<zstd> / event_utf8_bytes=原文长度）")

# 3.1 解出来必须**是原文**（长度 + 内容都一致）
lines3 = ad3.read_lines(ref3)
assert lines3 == EVENTS, "read_lines 解出的条目必须与压缩前的原文逐字段一致"
checked3 = ad3.read_lines_checked(ref3)
assert checked3.objs == EVENTS and not checked3.bad_lines, "read_lines_checked 不应把压缩事件算坏行"
print("✓ 真 zstd 解码：read_lines / read_lines_checked 读出与压缩前完全一致的原文")

# 3.2 create_fork 全链路：压缩源 → 分支（分支按明文落库，并带上原文长度）
r3 = create_fork(adapter=ad3, session_ref=SRC_ID, name="分支·压缩源",
                 backups_dir=os.path.join(root3, "backups"))
assert r3.ok and r3.verified, "含压缩事件的会话应能正常 fork"
con = sqlite3.connect(db3)
con.row_factory = sqlite3.Row
brows = con.execute(
    "SELECT seq,event_json,event_zstd,event_utf8_bytes FROM transcript_events "
    "WHERE session_id=? ORDER BY seq", (r3.new_id,)).fetchall()
assert len(brows) == len(EVENTS), f"分支事件数应为 {len(EVENTS)}，实际 {len(brows)}"
for r in brows:
    assert r["event_json"] is not None and r["event_zstd"] is None, \
        "分支事件一律写明文 event_json（event_zstd 留 NULL，满足 CHECK）"
    assert r["event_utf8_bytes"] == len(r["event_json"].encode("utf-8")), \
        "event_utf8_bytes 必须与写入的明文长度一致（读侧用它做长度校验）"
bobjs = [json.loads(r["event_json"]) for r in brows]
assert [o.get("id") for o in bobjs[1:]] == [o.get("id") for o in EVENTS[1:]], "条目 id 应保留"
assert bobjs[2]["message"]["content"][0]["text"] == "V3-OK", "压缩源的内容必须原样进分支"
con.close()
print("✓ create_fork：压缩源可 fork，分支逐条为明文 event_json + 正确 event_utf8_bytes")

# 3.3 体检：现在有解码能力 ⇒ 压缩事件应报 **ok** 且带统计（旧实现恒判红）
zok = {i.name: i for i in ad3.verify_storage()}["压缩事件 (event_zstd)"]
assert zok.ok is True, f"能解码时体检不应判红：{zok.detail}"
assert "2 条" in zok.detail and "解码" in zok.detail, zok.detail
print("✓ verify_storage：压缩事件 2 条已全部解码 ⇒ 报 ok（%s）" % zok.detail[:34])

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

# 3.6 两级解码库都不可用 ⇒ 报"带可执行修复建议"的错（用缓存隔离导入，不真卸包）
_saved_cache = ocs._ZSTD_CACHE
try:
    ocs._ZSTD_CACHE = (None, None)
    assert zstd_support() == (None, None), "隔离后应探测不到任何解码实现"
    try:
        ad3.read_lines(ref3)
        raise AssertionError("没有任何 zstd 实现时必须抛 ForkError，不能静默当坏行")
    except ForkError as e:
        msg = str(e)
        assert 'pip install "zstandard>=0.22"' in msg, msg
        assert "2 条" in msg, f"错误文案要说明该会话含几条压缩事件：{msg}"
    it = {i.name: i for i in ad3.verify_storage()}["压缩事件 (event_zstd)"]
    assert it.ok is False and "zstandard" in it.detail, it.detail
    print("✓ 两级解码库都缺 → ForkError 带修复建议（%s）；体检判红并给出同样建议"
          % 'pip install "zstandard>=0.22"')
finally:
    ocs._ZSTD_CACHE = _saved_cache

# 3.7 stdlib 分支（Python 3.14+ 的 compression.zstd）也要走通：
#     注入一个转调 zstandard 的假 stdlib 模块，验证"优先 stdlib"的选择逻辑真的生效
class _FakeStdlibZstd(types.ModuleType):
    def decompress(self, data, max_output_size=None):  # noqa: D401  （stdlib 同名接口）
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=max_output_size or 0)


_saved = {k: sys.modules.get(k) for k in ("compression", "compression.zstd")}
try:
    fake = types.ModuleType("compression")
    fake.zstd = _FakeStdlibZstd("compression.zstd")
    sys.modules["compression"] = fake
    sys.modules["compression.zstd"] = fake.zstd
    ocs._ZSTD_CACHE = None
    impl_name, dec = zstd_support()
    assert impl_name == "stdlib compression.zstd" and dec is not None, impl_name
    assert ad3.read_lines(ref3) == EVENTS, "stdlib 分支也应解出原文"
    print("✓ stdlib `compression.zstd` 可用时优先选它（真解出原文，不依赖 zstandard 路径）")
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
    ocs._ZSTD_CACHE = None

# ---------------------------------------------------------------------------
# 5. 旧 schema（无 event_zstd / event_utf8_bytes 列）仍可读 —— 不能因新列而崩
# ---------------------------------------------------------------------------
root6 = tempfile.mkdtemp(prefix="oc-contract-legacy-")
db6, ad6 = build_db(root6, {"sessionId": SRC_ID, "updatedAt": NOW})
con = sqlite3.connect(db6)
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
assert ad6.read_lines(make_ref(db6, SRC_ID)) == EVENTS, "旧 schema 应逐条读出原文"
lit = {i.name: i for i in ad6.verify_storage()}["压缩事件 (event_zstd)"]
assert lit.ok is True, f"没有 event_zstd 列的旧库不存在压缩事件，不该判红：{lit.detail}"
# 旧 schema 也能 fork：写入侧不能硬塞 event_utf8_bytes（那列不存在）
r6 = create_fork(adapter=ad6, session_ref=SRC_ID, name="分支·旧 schema",
                 backups_dir=os.path.join(root6, "backups"))
assert r6.ok and r6.verified, "旧 schema（无 event_zstd / event_utf8_bytes 列）应能 fork"
print("✓ 旧 schema（2026.9.x，无 event_zstd / event_utf8_bytes 列）仍可读可 fork")

print("\n✅ test_openclaw_sqlite_contract 全部通过"
      "（2026.11.3 三条契约 + zstd 真解码四情形 + 旧 schema 兼容）")

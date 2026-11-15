"""OpenClaw 2026.11.3 契约测试：entry_valid / 三条件血缘等式 / zstd 压缩事件。

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
  3. ≥1024B 的事件默认落成 `event_json = NULL, event_zstd = <zstd>`。本适配器未引入
     zstd 依赖 ⇒ 必须**明确报错**，绝不能静默当成坏行/空行（否则静默产出缺事件的
     坏分支，比直接失败危险得多）。

与 test_openclaw_sqlite_adapter.py 的分工：那个文件固化"写入 7 张表"的主流程，
本文件固化上面三条 2026.11.3 特有契约。
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core.adapter_openclaw_sqlite import OpenClawSqliteAdapter, make_ref  # noqa: E402
from fork_core.engine import ForkError, create_fork  # noqa: E402

SRC_ID = "bbbbbbbb-1111-2222-3333-444444444444"
SRC_KEY = "agent:main:lab-with-spawn"
NOW = 1800000000000

# 2026.11.3 的 DDL 最小子集（7 张表 + event_zstd 列 + entry_valid CHECK）。
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
  created_at INTEGER NOT NULL, event_zstd BLOB, PRIMARY KEY (session_id, seq),
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


def build_db(root, src_entry, *, zstd_seq=None):
    """造一个 2026.11.3 形状的最小库；返回 (db_path, adapter)。

    zstd_seq：把该 seq 的事件改成 `event_json = NULL, event_zstd = <bytes>`
    （模拟 ≥1024B 事件被产品压缩落盘）。
    """
    agent_dir = os.path.join(root, "state", "agents", "main")
    os.makedirs(os.path.join(agent_dir, "agent"), exist_ok=True)
    db = os.path.join(agent_dir, "agent", "openclaw-agent.sqlite")
    con = sqlite3.connect(db)
    con.executescript(DDL)
    for seq, o in enumerate(EVENTS):
        if seq == zstd_seq:
            con.execute(
                "INSERT INTO transcript_events (session_id,seq,event_json,created_at,event_zstd) "
                "VALUES (?,?,NULL,?,?)", (SRC_ID, seq, NOW, b"\x28\xb5\x2f\xfd" + b"\x00" * 64))
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
# 3. zstd 压缩事件：必须抛可读错，不能静默当成坏行/空行
# ---------------------------------------------------------------------------
root3 = tempfile.mkdtemp(prefix="oc-contract-zstd-")
db3, ad3 = build_db(root3, {"sessionId": SRC_ID, "updatedAt": NOW}, zstd_seq=2)
ref3 = make_ref(db3, SRC_ID)

for label, fn in (("read_lines", ad3.read_lines), ("read_lines_checked", ad3.read_lines_checked)):
    try:
        fn(ref3)
        raise AssertionError(f"{label} 读到 event_json=NULL 时必须抛 ForkError，不能静默")
    except ForkError as e:
        msg = str(e)
        assert "zstd" in msg and "event_zstd" in msg, msg
        assert "未启用 zstd 解码" in msg, msg
print("✓ event_json IS NULL（zstd）→ read_lines / read_lines_checked 抛可读 ForkError")

try:
    create_fork(adapter=ad3, session_ref=SRC_ID, name="分支·不该出现",
                backups_dir=os.path.join(root3, "backups"))
    raise AssertionError("含 zstd 事件的会话不应被静默 fork 成功")
except ForkError as e:
    assert "zstd" in str(e)
assert not os.path.exists(os.path.join(root3, "state", "agents", "main", "fork.lineage.json")) \
    or not json.load(open(os.path.join(root3, "state", "agents", "main", "fork.lineage.json"),
                          encoding="utf-8")).get("forks")
print("✓ create_fork 前置读取即失败（无静默产物、无谱系登记）")

zitems = {i.name: i for i in ad3.verify_storage()}
assert zitems["压缩事件 (event_zstd)"].ok is False, "有压缩事件时体检必须报出来"
assert "zstd" in zitems["压缩事件 (event_zstd)"].detail
print("✓ verify_storage：压缩事件项如实报红（%s）" % zitems["压缩事件 (event_zstd)"].detail[:28])

# ---------------------------------------------------------------------------
# 4. 旧 schema（无 event_zstd 列）仍可读 —— 不能因新列而崩
# ---------------------------------------------------------------------------
root4 = tempfile.mkdtemp(prefix="oc-contract-legacy-")
db4, ad4 = build_db(root4, {"sessionId": SRC_ID, "updatedAt": NOW})
con = sqlite3.connect(db4)
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
assert len(ad4.read_lines(make_ref(db4, SRC_ID))) == len(EVENTS)
print("✓ 旧 schema（2026.9.x，无 event_zstd 列）仍能读（查询按实际列集自适应）")

print("\n✅ test_openclaw_sqlite_contract 全部通过（2026.11.3 三条契约 + 旧 schema 兼容）")

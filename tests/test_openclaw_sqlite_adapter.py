"""OpenClaw ≥2026.9.x（SQLite 后端）契约测试（fixture 级，不依赖真实 OpenClaw）。

固化的契约来自一手真机证据（2026-09-14，可本地复跑核对）：
  - 真机 openclaw@2026.9.4 的 openclaw-agent.sqlite 实测表结构
  - 实测踩出的两条坑：缺 session_transcript_index_state → "projection is rebuilding"；
    直接写入的 session_nodes 会被触发器置 entry_valid=0 → 需官方 doctor 校验

本测试只固化**本适配器的写入契约**（7 张表 / 行数 / header 替换 / 备份介质）。
"产物能被真机认"属 L3 产品终验，由 lab 下的真机脚本负责，不在单元测试里伪造。
"""
import json
import os
import sqlite3
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC_ID = "aaaaaaaa-1111-2222-3333-444444444444"
SRC_KEY = "agent:main:lab"

tmpdir = tempfile.mkdtemp(prefix="oc-sqlite-test-")
state = os.path.join(tmpdir, "state")
agent_dir = os.path.join(state, "agents", "main")
os.makedirs(os.path.join(agent_dir, "agent"), exist_ok=True)
DB = os.path.join(agent_dir, "agent", "openclaw-agent.sqlite")
os.environ["OPENCLAW_STATE_DIR"] = state

# ---------- 造一个最小但结构真实的库 ----------
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE session_windows (
  session_id TEXT NOT NULL PRIMARY KEY, session_key TEXT NOT NULL,
  previous_session_id TEXT, reason TEXT, session_scope TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  transcript_updated_at INTEGER, transcript_observed_at INTEGER, status TEXT
) STRICT;
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL,
  created_at INTEGER NOT NULL, PRIMARY KEY (session_id, seq)
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
  entry_json TEXT NOT NULL, entry_valid INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL, created_at INTEGER,
  fork_source_session_key TEXT, fork_source_session_id TEXT, fork_source_entry_id TEXT,
  parent_session_key TEXT
) STRICT;
-- 真机上的触发器：任何写入都把 entry_valid 置 0（校验权归产品）
CREATE TRIGGER session_nodes_entry_valid_after_insert
AFTER INSERT ON session_nodes BEGIN
  UPDATE session_nodes SET entry_valid = 0 WHERE session_key = NEW.session_key;
END;
""")

now = 1800000000000
EVENTS = [
    {"type": "session", "version": 3, "id": SRC_ID, "timestamp": "2026-09-14T06:00:00.000Z", "cwd": "/tmp/ws"},
    {"type": "model_change", "id": "mc01", "parentId": None, "provider": "ollama", "modelId": "qwen2.5:3b"},
    {"type": "custom", "customType": "openclaw:bootstrap-context:full", "id": "cu01", "parentId": "mc01",
     # 历史 run 记录：含源会话 id，属应当保留的原始内容
     "data": {"timestamp": now, "runId": "run-1", "sessionId": SRC_ID}},
    {"type": "message", "id": "m001", "parentId": "cu01",
     "message": {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: V9-OK"}]}},
    {"type": "message", "id": "m002", "parentId": "m001",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "V9-OK"}]}},
    {"type": "custom", "customType": "openclaw.cache-ttl", "id": "cu02", "parentId": "m002", "data": {"ttl": 1}},
]
for seq, o in enumerate(EVENTS):
    con.execute("INSERT INTO transcript_events VALUES (?,?,?,?)",
                (SRC_ID, seq, json.dumps(o, ensure_ascii=False), now))
    con.execute("INSERT INTO transcript_event_identities VALUES (?,?,?,?,?,?,?)",
                (SRC_ID, o["id"], seq, o["type"], o.get("parentId"), None, now))
    if seq > 0:  # header 不进投影（真机实测：31 事件 / 30 投影行）
        mp = None
        if o["type"] == "message":
            mp = sum(1 for x in EVENTS[1:seq + 1] if x["type"] == "message") - 1
        con.execute("INSERT INTO session_transcript_active_events VALUES (?,?,?,?,?)",
                    (SRC_ID, seq - 1, seq, mp, 1))
con.execute("INSERT INTO session_windows VALUES (?,?,?,?,?,?,?,?,?,?)",
            (SRC_ID, SRC_KEY, None, None, "conversation", now, now, now, now, None))
con.execute("INSERT INTO transcript_rewrite_watermarks VALUES (?,?,?)", (SRC_ID, "gen1", now))
msg_cnt = sum(1 for o in EVENTS[1:] if o["type"] == "message")
con.execute("INSERT INTO session_transcript_index_state VALUES (?,?,?,0,?,?,?)",
            (SRC_ID, len(EVENTS) - 1, EVENTS[-1]["id"], len(EVENTS) - 1, msg_cnt, now))
con.execute("INSERT INTO session_nodes VALUES (?,?,?,1,?,?,?,?,?,?)",
            (SRC_KEY, SRC_ID, json.dumps({"sessionId": SRC_ID, "updatedAt": now}), now, now,
             None, None, None, None))
con.commit()
con.close()

from fork_core.adapter_openclaw_sqlite import OpenClawSqliteAdapter, make_ref, parse_ref  # noqa: E402
from fork_core.engine import create_fork  # noqa: E402

adapter = OpenClawSqliteAdapter()

# 1. 可用性探测（选后端的判据）
assert adapter.is_available(), "有 transcript_events 行时应判定可用"
print("✓ is_available 探测（按 transcript_events 是否有行）")

# 2. 定位与引用格式
ref, slug = adapter.find_transcript(SRC_ID)
assert ref == make_ref(DB, SRC_ID), ref
assert parse_ref(ref) == (DB, SRC_ID)
assert adapter.resolve_session("current") == SRC_ID
assert [r[1] for r in adapter.list_all_sessions()] == [SRC_ID]
print("✓ 定位 / 引用格式 <db>#<session_id>")

# 3. 读：按 seq 排序，与 JSONL 的「行序 → 条目」同构
lines = adapter.read_lines(ref)
assert [o["type"] for o in lines] == [o["type"] for o in EVENTS]
assert lines[0]["id"] == SRC_ID and lines[0]["type"] == "session"
print("✓ read_lines 按 seq 排序（契约与 JSONL 同构）")

# 4. 反查：事件 id → 会话
assert adapter.find_session_by_request_id("m001") == SRC_ID
assert adapter.find_session_by_request_id("不存在的 id") is None
print("✓ find_session_by_request_id（查 transcript_event_identities）")

# 5. 全链路 create_fork
before = os.path.getsize(DB)
r = create_fork(adapter=adapter, session_ref=SRC_ID, name="分支·SQLite",
                backups_dir=os.path.join(tmpdir, "backups"))
assert r.ok and r.verified, "全链路 fork 失败"
assert r.cut == 5, f"cut={r.cut}（末条 assistant 在 L5）"
print("✓ 全链路 create_fork（SQLite 介质）")

# 6. 产物 = 同库新 session_id（不是新文件）
assert parse_ref(r.dst_path)[0] == DB and parse_ref(r.dst_path)[1] == r.new_id
print("✓ 产物落点：同库、新 session_id（非文件）")

# 7. 【核心】7 张表都要写（缺 session_transcript_index_state → projection is rebuilding）
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
NEW = r.new_id


def cnt(sql, *a):
    return con.execute(sql, a).fetchone()[0]


assert cnt("SELECT COUNT(*) FROM session_windows WHERE session_id=?", NEW) == 1
assert cnt("SELECT COUNT(*) FROM transcript_events WHERE session_id=?", NEW) == r.cut
assert cnt("SELECT COUNT(*) FROM transcript_event_identities WHERE session_id=?", NEW) == r.cut
assert cnt("SELECT COUNT(*) FROM session_transcript_active_events WHERE session_id=?", NEW) == r.cut - 1
assert cnt("SELECT COUNT(*) FROM transcript_rewrite_watermarks WHERE session_id=?", NEW) == 1
assert cnt("SELECT COUNT(*) FROM session_transcript_index_state WHERE session_id=?", NEW) == 1
assert cnt("SELECT COUNT(*) FROM session_nodes WHERE current_session_id=?", NEW) == 1
print("✓【7 张表】全部写入（窗口/事件/身份/投影/水印/投影进度/节点）")

# 8. 投影进度自洽（indexed_seq / leaf_event_id / 计数）
st = con.execute("SELECT * FROM session_transcript_index_state WHERE session_id=?", (NEW,)).fetchone()
assert st["indexed_seq"] == r.cut - 1, st["indexed_seq"]
assert st["leaf_event_id"] == EVENTS[r.cut - 1]["id"], st["leaf_event_id"]
assert st["active_event_count"] == cnt(
    "SELECT COUNT(*) FROM session_transcript_active_events WHERE session_id=?", NEW)
assert st["needs_rebuild"] == 0
print("✓ 投影进度自洽（indexed_seq / leaf_event_id / active_*_count / needs_rebuild=0）")

# 9. 节点：entry_valid 归产品校验（触发器置 0），谱系字段已写
node = con.execute("SELECT * FROM session_nodes WHERE current_session_id=?", (NEW,)).fetchone()
assert node["entry_valid"] == 0, "entry_valid 必须留 0（交由 openclaw doctor 校验）"
assert node["fork_source_session_id"] == SRC_ID
assert node["parent_session_key"] == SRC_KEY
assert node["session_key"] == f"agent:main:fork-{NEW[:8]}"
assert SRC_ID not in node["entry_json"], "索引条目里不应残留源 id"
print("✓ session_nodes：entry_valid=0（待产品校验）+ 原生谱系字段")

# 10. 分支转录内容 = 源前缀，且 header.id 已换、条目 id 保留
out = adapter.read_lines(r.dst_path)
assert len(out) == r.cut
assert out[0]["id"] == NEW and out[0]["version"] == 3
assert [o["id"] for o in out[1:]] == [o["id"] for o in EVENTS[1:r.cut]], "条目 id 是树边，不得改动"
print("✓ 分支转录：header.id 已换 / 条目 id 保留")

# 11. data.sessionId 不参与替换（历史 run 记录；且不触发误拒）
assert out[2]["data"]["sessionId"] == SRC_ID, "custom 条目的历史 run 记录应原样保留"
assert "data" in adapter._RAW_KEYS
print("✓ data（custom 条目原始记录）列入 _RAW_KEYS，不做替换、不误报残留")

# 12. 备份是「整个数据库」
baks = []
for root, _, files in os.walk(os.path.join(tmpdir, "backups")):
    baks += [os.path.join(root, f) for f in files if f.endswith(".sqlite")]
assert baks, "未发现数据库备份"
assert os.path.getsize(baks[0]) > 0
assert adapter.backup_note() != "source jsonl only, no database copy"
print("✓ backup_transcript 备份整个数据库（%d 字节）" % os.path.getsize(baks[0]))

# 13. 文案：无文件可锁 → 不提示 chmod；有 doctor 步骤提示
assert adapter.readonly_hint(r.dst_path) == "", "SQLite 分支不是文件，不应提示 chmod"
assert adapter.publish_hint() == "openclaw doctor --fix"
print("✓ 文案产品化（readonly_hint 为空 / publish_hint=doctor --fix）")

# 14. 体检
items = {i.name: i for i in adapter.verify_storage()}
assert items["OpenClaw SQLite 转录库"].level == "L2"
assert items["行存储契约 (session_id, seq) → event_json"].ok
print("✓ verify_storage 三层")

# 15. 注销：连带清掉库里的节点与窗口
adapter.unregister_branch(NEW)
assert cnt("SELECT COUNT(*) FROM session_nodes WHERE current_session_id=?", NEW) == 0
assert cnt("SELECT COUNT(*) FROM session_windows WHERE session_id=?", NEW) == 0
assert NEW not in {b.id for b in adapter.list_branches()}
print("✓ unregister_branch 清理库内节点/窗口 + 旁路索引")

con.close()
print("\n✅ test_openclaw_sqlite_adapter 全部通过（15 组断言）")

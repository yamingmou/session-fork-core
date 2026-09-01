import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, "/Users/maxwell/.workbuddy/skills/session-fork/scripts")

import fork_core.adapters.workbuddy as wb_mod
from fork_core.adapters.workbuddy import WorkBuddyAdapter
from fork_core.models import SessionMeta

# --- 临时 db + 临时 projects ---
tmpdir = tempfile.mkdtemp(prefix="wb-test-")
db_path = os.path.join(tmpdir, "workbuddy.db")
proj_dir = os.path.join(tmpdir, "projects", "test-workspace")
os.makedirs(proj_dir, exist_ok=True)

wb_mod.DB_PATH = db_path
wb_mod.PROJECTS_DIR = os.path.join(tmpdir, "projects")

# 造源会话行（模拟 sessions 表结构）
conn = sqlite3.connect(db_path)
conn.execute(
    """CREATE TABLE sessions (
        id TEXT, custom_title TEXT, status TEXT, created_at INTEGER,
        updated_at INTEGER, last_activity_at INTEGER, cwd TEXT)"""
)
src_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
now = 1786000000000
conn.execute(
    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
    (src_id, "测试源会话", "working", now, now, now, "/tmp/test"),
)
conn.commit()
conn.close()

# 造 transcript 文件
lines = [
    {"type": "message", "role": "user", "sessionId": src_id,
     "content": [{"type": "input_text", "text": "你好"}]},
    {"type": "message", "role": "assistant", "sessionId": src_id,
     "content": [{"type": "output_text", "text": "你好！我是助手"}]},
    {"type": "message", "role": "user", "sessionId": src_id,
     "content": [{"type": "input_text", "text": "打分支"}]},
]
with open(os.path.join(proj_dir, src_id + ".jsonl"), "w") as f:
    for l in lines:
        f.write(json.dumps(l) + "\n")

adapter = WorkBuddyAdapter()

# 1. find + meta
p, s = adapter.find_transcript(src_id)
assert p, "transcript not found"
meta = adapter.load_session_meta(src_id)
assert meta.id == src_id and meta.title == "测试源会话"
print("✓ find_transcript + load_session_meta")

# 2. 定位
from fork_core.engine import create_fork
r = create_fork(adapter, src_id, name="WB测试分支", dry_run=False)
assert r.ok and os.path.exists(r.dst_path)
print(f"✓ create_fork: {r.new_id} cut={r.cut} name={r.name!r}")

# 3. register_branch 写入 db
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM sessions WHERE id=?", (r.new_id,)).fetchone()
conn.close()
assert row is not None, "branch row not in db"
assert row["custom_title"] == "WB测试分支"
assert row["status"] == "terminated"
print(f"✓ register_branch: db 行已写入 status={row['status']} title={row['custom_title']}")

# 4. list_branches
branches = adapter.list_branches()
assert any(b.id == r.new_id for b in branches), "branch not listed"
print(f"✓ list_branches 找到新分支（共 {len(branches)} 个）")

# 5. 源会话未被污染
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM sessions WHERE id=?", (src_id,)).fetchone()
conn.close()
assert row["status"] == "working"
print("✓ 源会话 status 未变（working）")

# 6. 谱系记录（旁路索引）
lineage = adapter._lineage_get()
assert r.new_id in lineage, "fork 未写入谱系索引"
assert lineage[r.new_id]["parent_id"] == src_id, "谱系 parent 错误"
assert lineage[r.new_id]["at_seq"] == r.cut, f"谱系 at_seq 错误: {lineage[r.new_id]['at_seq']} != {r.cut}"
print(f"✓ 谱系索引: {r.new_id[:8]}… parent={src_id[:8]}… at_seq={r.cut}")

# 7. list_branches 带谱系
branches2 = adapter.list_branches()
b2 = next(b for b in branches2 if b.id == r.new_id)
assert b2.parent_id == src_id, "list_branches 未补 parent_id"
assert b2.extra.get("at_seq") == r.cut, "list_branches 未补 at_seq"
print(f"✓ list_branches 谱系补齐: parent={b2.parent_id[:8]}… at_seq={b2.extra.get('at_seq')}")

# 8. 从分支再 fork（快照点可回：新投影自身也可再派生）
r2 = create_fork(adapter, r.new_id, name="孙分支", dry_run=False)
lineage2 = adapter._lineage_get()
assert r2.new_id in lineage2, "孙分支未写入谱系"
assert lineage2[r2.new_id]["parent_id"] == r.new_id, "孙分支 parent 错误"
print(f"✓ 从分支再 fork: {r2.new_id[:8]}… parent={r.new_id[:8]}… at_seq={r2.cut}")

# 9. lineage_tree
tree = adapter.lineage_tree()
tree_ids = [t.id for t in tree]
assert src_id in tree_ids and r.new_id in tree_ids and r2.new_id in tree_ids, "谱系树缺节点"
src_node = next(t for t in tree if t.id == src_id)
assert not src_node.parent_id, "根节点不应有 parent"
print(f"✓ lineage_tree: {len(tree)} 节点（根→分支→孙分支）")

import shutil
shutil.rmtree(tmpdir)
print("\n✅ WorkBuddy adapter 全部测试通过（含谱系/再 fork/谱系树）")

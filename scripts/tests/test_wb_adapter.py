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

import shutil
shutil.rmtree(tmpdir)
print("\n✅ WorkBuddy adapter 全部测试通过（与 v1.4.2 行为一致）")

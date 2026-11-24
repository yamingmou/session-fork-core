#!/usr/bin/env python3
"""OpenCode 适配器契约测试（自带夹具，不需要本机装 opencode）。

覆盖四件事，每条都对应一个实测踩过的坑：
  ① 读：`session_message` 按 `seq` 排序，user/assistant 文本取对；
  ② 写分支：**只写新一代** `session_v2` + `session_message`（旧表不写）；
  ③ **parent 必须挂到源会话**——第一版记在 register_branch 里，那时拿不到源，
     退回"最新会话"就把分支挂到了用户当时最新的**另一条**会话上；
  ④ **必须补 `event_sequence` 发号器**——不补，产品从低位发号、撞上已有序号，
     界面表现是"输入新消息一直转圈（工作中）"。
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core.adapter_opencode import OpenCodeAdapter, make_ref   # noqa: E402
from fork_core.models import SessionMeta                            # noqa: E402

SRC = "ses_SRC0000000000000000000"


def build_db(path: str, *, sessions: list[str]) -> None:
    """造一个最小可用的 opencode 库（形状照真机）。"""
    con = sqlite3.connect(path)
    con.executescript("""
    create table project (id text primary key, worktree text, time_created integer, time_active integer);
    create table session_v2 (
        id text primary key, project_id text, workspace_id text, parent_id text,
        fork_session_id text, fork_boundary text, slug text, directory text, path text,
        title text, version text, share_url text, summary_additions integer, summary_deletions integer,
        summary_files integer, summary_diffs text, metadata text, cost real, tokens_input integer,
        tokens_output integer, tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer,
        revert text, permission text, agent text, model text, time_created integer, time_updated integer,
        time_compacting integer, time_archived integer, time_suspended integer, resume_attempts integer,
        time_idle integer, time_viewed integer, idle_outcome text, time_busy integer);
    create table session_message (
        id text primary key, session_id text, type text, seq integer,
        time_created integer, time_updated integer, data text);
    create table event_sequence (aggregate_id text primary key, seq integer, owner_id text);
    """)
    con.execute("insert into project values ('proj1', '/tmp/proj', 0, 0)")
    for sid in sessions:
        con.execute(
            "insert into session_v2 (id, project_id, directory, title, version, agent, model, "
            "time_created, time_updated, time_busy, time_idle, time_viewed, idle_outcome, cost, "
            "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "proj1", "/tmp/proj", "源会话", "2.2.7", "build",
             json.dumps({"id": "m", "providerID": "p"}), 1000, 2000, 1000, 2000, 2000, "succeeded",
             0, 0, 0, 0, 0, 0))
    if SRC in sessions:
        rows = [("user", json.dumps({"text": "第一问"})),
                ("assistant", json.dumps({"content": [{"type": "text", "text": "第一答"}]})),
                ("user", json.dumps({"text": "第二问"})),
                ("assistant", json.dumps({"content": [{"type": "text", "text": "第二答"}]}))]
        for i, (typ, data) in enumerate(rows, 1):
            con.execute("insert into session_message values (?,?,?,?,?,?,?)",
                        (f"msg_s{i:04d}", SRC, typ, i, 1000 + i, 1000 + i, data))
        con.execute("insert into event_sequence values (?,?,NULL)", (SRC, 5))
    con.commit()
    con.close()


tmp = tempfile.mkdtemp(prefix="oc-adapter-")
db = os.path.join(tmp, "opencode.db")
build_db(db, sessions=[SRC])

ad = OpenCodeAdapter()
ad.DB_PATH = db

# ① 读
ref, cwd = ad.find_transcript(SRC)
assert ref == make_ref(db, SRC), ref
assert cwd == "/tmp/proj", cwd
lines = ad.read_lines(ref)
assert len(lines) == 4, len(lines)
assert ad.is_user_message(lines[0]) and ad.get_text(lines[0]) == "第一问"
assert ad.is_assistant_message(lines[1]) and ad.get_text(lines[1]) == "第一答"
print("✓ 读：4 行，user/assistant 文本取对（assistant 从 content 块拼）")

# ② 分支落位
new_id = ad.new_session_id()
assert new_id.startswith("ses_") and len(new_id) == 28, new_id
dst = ad.branch_target(ref, new_id)
kept = lines[:2]
kept, n = ad.rewrite_ids(kept, SRC, new_id)
assert n == 2, n
tmp_ref = f"{dst}.{os.getpid()}.tmp"
ad.write_branch(tmp_ref, kept)
# 引擎在 finalize 之前就要读临时引用做校验
assert len(ad.read_lines(tmp_ref)) == 2
ad.finalize_branch(tmp_ref, dst)
ad.register_branch(SessionMeta(id=SRC, title="源会话"), new_id, dst, "分支名")

con = sqlite3.connect(db)
row = con.execute("select id, title, parent_id, project_id from session_v2 where id=?",
                  (new_id,)).fetchone()
assert row is not None, "分支行没写进去"
assert row[1] == "分支名", row
assert row[2] == SRC, f"parent 必须是源会话（实际 {row[2]}）"
assert row[3] == "proj1", "project_id 必须沿用源会话（否则产品按目录过滤看不到）"
n_msg = con.execute("select count(*) from session_message where session_id=?", (new_id,)).fetchone()[0]
assert n_msg == 2, n_msg
n_legacy = con.execute("select count(*) from session where id=?", (new_id,)).fetchone()[0] \
    if con.execute("select 1 from sqlite_master where name='session'").fetchone() else 0
assert n_legacy == 0
print("✓ 分支落位：只写新一代；parent=源会话；project_id 沿用；2 条消息")

# ④ 发号器
seq = con.execute("select seq from event_sequence where aggregate_id=?", (new_id,)).fetchone()
assert seq is not None, "缺 event_sequence 行 —— 产品会一直转圈"
assert seq[0] >= n_msg, seq
print(f"✓ 发号器：event_sequence={seq[0]}（必须 ≥ 已用最大 seq）")
con.close()

# ⑤ 备份走 SQLite 官方 backup（不是 cp，避免丢 -wal）
bd = ad.backup_transcript(ref, os.path.join(tmp, "backups"))
assert os.path.exists(os.path.join(bd, "opencode.db")), bd
print("✓ 备份：整个库的一致性快照")

# ⑥ unregister 能干净撤销
ad.unregister_branch(new_id)
con = sqlite3.connect(db)
assert con.execute("select count(*) from session_v2 where id=?", (new_id,)).fetchone()[0] == 0
assert con.execute("select count(*) from session_message where session_id=?", (new_id,)).fetchone()[0] == 0
assert con.execute("select count(*) from event_sequence where aggregate_id=?", (new_id,)).fetchone()[0] == 0
con.close()
print("✓ 撤销：三张表都清干净")

print("\n✅ OpenCode 适配器契约测试通过")

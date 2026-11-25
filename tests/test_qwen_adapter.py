#!/usr/bin/env python3
"""Qwen 适配器契约测试（自带夹具，不需要本机装 Qwen）。

判据：
  ① 读：按 sequence 排，parts 里只要文本块（tool-* / error 跳过）；
  ② 写分支：chats + sub_chats + messages 三张表都要有行（少一张界面认不全）；
  ③ ⭐ searchable_text 必须填 —— FTS 触发器只认它，不填则"消息可见但搜不到"；
  ④ 时间戳按秒；
  ⑤ parent（source_chat_id）必须是源会话。
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core.adapter_qwen import QwenAdapter, make_ref      # noqa: E402
from fork_core.models import SessionMeta                       # noqa: E402

SRC = "mwsrc000000000"
SCHEMA = """
create table chats (id text primary key not null, name text, project_id text not null,
  created_at integer, updated_at integer, archived_at integer, worktree_path text, branch text,
  base_branch text, pr_url text, pr_number integer, additional_directories text,
  output_directory text, source text, wecom_account_id text, wecom_user_id text,
  chat_type text default 'task', ext text, source_chat_id text, deleted_at integer,
  local_project_id text, version integer default 0,
  session_namespace text not null default 'standard', identity_key text);
create table sub_chats (id text primary key not null, name text, chat_id text not null,
  session_id text, mode text not null default 'agent', messages text not null default '[]',
  created_at integer, updated_at integer, stream_id text, feedback text default 'none',
  model_level text, ext text, message_feedbacks text default '{}');
create table messages (id text primary key not null, message_id text not null,
  chat_id text not null, sub_chat_id text not null, sequence integer not null,
  role text not null, parts text not null default '[]', metadata text not null default '{}',
  searchable_text text, search_status text not null default 'ready',
  created_at integer, updated_at integer);
create virtual table messages_fts using fts5(searchable_text, chat_id, sub_chat_id, message_id, role);
create trigger messages_fts_ai after insert on messages begin
  insert into messages_fts(rowid, searchable_text, chat_id, sub_chat_id, message_id, role)
  values (new.rowid, new.searchable_text, new.chat_id, new.sub_chat_id, new.message_id, new.role);
end;
"""

tmp = tempfile.mkdtemp(prefix="qw-adapter-")
db = os.path.join(tmp, "agents.db")
con = sqlite3.connect(db)
con.executescript(SCHEMA)
con.execute("insert into chats (id,name,project_id,created_at,updated_at,worktree_path) "
            "values (?,?,?,?,?,?)", (SRC, "源会话", "proj1", 1700000000, 1700000000, "/tmp/proj"))
con.execute("insert into sub_chats (id,chat_id,session_id,created_at,updated_at) values "
            "('mwsub000000000',?, 'sess-1', 1700000000, 1700000000)", (SRC,))
rows = [("user", [{"type": "text", "text": "第一问"}]),
        ("assistant", [{"type": "tool-Thinking", "toolCallId": "t1"},
                       {"type": "text", "text": "第一答"}]),
        ("assistant", [{"type": "error", "text": "402"}]),
        ("user", [{"type": "text", "text": "第二问"}])]
for i, (role, parts) in enumerate(rows):
    con.execute("insert into messages (id,message_id,chat_id,sub_chat_id,sequence,role,parts,metadata,"
                "searchable_text,created_at,updated_at) values (?,?,?,?,?,?,?,'{}',?,?,?)",
                (f"msg{i:06d}", f"mid{i:06d}", SRC, "mwsub000000000", i, role,
                 json.dumps(parts, ensure_ascii=False), "", 1700000000 + i, 1700000000 + i))
con.commit()
con.close()

ad = QwenAdapter()
ad.DB_PATH = db

# ① 读
ref, cwd = ad.find_transcript(SRC)
assert ref == make_ref(db, SRC) and cwd == "/tmp/proj", (ref, cwd)
lines = ad.read_lines(ref)
assert len(lines) == 4, len(lines)
assert ad.is_user_message(lines[0]) and ad.get_text(lines[0]) == "第一问"
assert ad.get_text(lines[1]) == "第一答", "tool-* 块应被跳过，只留文本"
assert ad.get_text(lines[2]) == "", "error 块不该算对话内容"
print("✓ 读：4 行；tool-* 与 error 跳过，只留文本")

# ② 写分支
new_id = ad.new_session_id()
assert new_id.startswith("mw") and len(new_id) == 16, new_id
dst = ad.branch_target(ref, new_id)
kept, n = ad.rewrite_ids(lines[:2], SRC, new_id)
assert n == 2, n
tmp_ref = f"{dst}.{os.getpid()}.tmp"
ad.write_branch(tmp_ref, kept)
assert len(ad.read_lines(tmp_ref)) == 2, "finalize 之前引擎要能读临时引用做校验"
ad.finalize_branch(tmp_ref, dst)
ad.register_branch(SessionMeta(id=SRC, title="源会话"), new_id, dst, "分支名")

con = sqlite3.connect(db)
assert con.execute("select count(*) from chats where id=?", (new_id,)).fetchone()[0] == 1
assert con.execute("select count(*) from sub_chats where chat_id=?", (new_id,)).fetchone()[0] == 1
n_msg = con.execute("select count(*) from messages where chat_id=?", (new_id,)).fetchone()[0]
assert n_msg == 2, n_msg
print("✓ 写：chats + sub_chats + messages 三张表都有行")

# ③ searchable_text → FTS
n_fts = con.execute("select count(*) from messages_fts where chat_id=?", (new_id,)).fetchone()[0]
assert n_fts == n_msg, f"FTS 未同步 {n_fts}/{n_msg}"
print(f"✓ 搜索：searchable_text 已填，FTS {n_fts}/{n_msg} 同步")

# ④ 时间戳按秒
ts = con.execute("select created_at from messages where chat_id=? order by sequence limit 1",
                 (new_id,)).fetchone()[0]
assert ts == 1700000000, ts
print(f"✓ 时间戳按秒：{ts}")

# ⑤ parent
parent = con.execute("select source_chat_id from chats where id=?", (new_id,)).fetchone()[0]
assert parent == SRC, f"parent 必须是源会话（实际 {parent}）"
print("✓ parent（source_chat_id）= 源会话")

# ⑥ 撤销
ad.unregister_branch(new_id)
for t, col in (("chats", "id"), ("sub_chats", "chat_id"), ("messages", "chat_id")):
    assert con.execute(f"select count(*) from {t} where {col}=?", (new_id,)).fetchone()[0] == 0, t
con.close()
print("✓ 撤销：三张表都清干净")

print("\n✅ Qwen 适配器契约测试通过")

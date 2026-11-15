"""Hermes 适配器契约测试（fixture 级，不依赖本机 Hermes 安装）。

固化的一手证据来自 2026-09-14 真机取证 + 2026-09-15 适配器落库验收：
  - 2026-09-14 真机取证记录（含全部命令与原始输出）
  - 2026-09-15 落库验收：产品列出 + 续跑并答对被复制前缀里的信息

**本文件里的 DDL 是从真机 `state.db`（v0.21.2 / SCHEMA_VERSION=30）逐字取出的**
（由生成器注入，非手抄），只省略与适配器**无关**的对象：
`messages_fts*` 虚表及其触发器（适配器必须**不依赖** FTS —— 这条本身就是要测的性质）、
以及显示层的两个 display_identity 触发器（不影响写入）。
每条断言都对应一个实测事实，注释写明出处。
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ HERMES_HOME 必须在构造 adapter 前设好（__init__ 读环境变量）
tmpdir = tempfile.mkdtemp(prefix="hermes-test-")
os.environ["HERMES_HOME"] = tmpdir

from fork_core.adapters import get_adapter  # noqa: E402
from fork_core.engine import create_fork, verify_branch  # noqa: E402

DB = os.path.join(tmpdir, "state.db")
BK = os.path.join(tmpdir, "backups")   # 备份也留在隔离根内，别写进 ~/.workbuddy/backups

# ---------------------------------------------------------------- 真实 DDL
SESSIONS_DDL = """CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    title_source TEXT,
    last_activity_at REAL,
    last_activity_description TEXT,
    last_activity_provenance TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    compression_recovery_deadline REAL,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_read_at REAL,
    tool_names TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_prompt_hash) REFERENCES system_prompts(hash)
)"""

MESSAGES_DDL = """CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    _compressed_summary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT,
    display_identity BLOB,
    display_order INTEGER
)"""

SYSTEM_PROMPTS_DDL = """CREATE TABLE system_prompts (
    hash TEXT PRIMARY KEY,
    prompt TEXT NOT NULL
)"""

EXTRA_DDL = [
    """CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL""",
    """CREATE INDEX idx_messages_session_id ON messages(session_id, id)""",
    """CREATE TRIGGER messages_display_order_insert
AFTER INSERT ON messages WHEN new.display_order IS NULL
BEGIN
    UPDATE messages SET display_order = COALESCE((
        SELECT display_order FROM messages
        WHERE session_id = new.session_id AND id <> new.id
          AND (active = 1 OR compacted = 1)
          AND display_identity = new.display_identity AND display_order IS NOT NULL
        ORDER BY display_order LIMIT 1
    ), new.id) WHERE id = new.id;
END""",
]


#: v0.21.6（schema **31**）新增的列 —— 与真机迁移**逐字同形**（`ALTER TABLE … ADD COLUMN`）。
#: 出处：2026-10-09 用产品自己的 `hermes_state.SessionDB` 把 v30 真库迁到 v31 后，
#: 用 `PRAGMA table_info` 实测得到（`sessions` 58→62、`messages` 26→30）。
V31_NEW_COLUMNS = {
    "sessions": [
        ("created_source", "TEXT"),
        ("compression_overload_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("transport_profile", "TEXT"),
        ("auto_archived", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "messages": [
        ("message_uid", "TEXT"),
        ("absorbed_message_uids", "TEXT"),
        ("tool_call_uids", "TEXT"),
        ("tool_call_uid", "TEXT"),
    ],
}

#: v0.23.1（schema **32/33**）新增的列 —— 出处是上游 tag `v0.23.1`（commit `3b42157`）的
#: `hermes_state_common.py` 里 `SCHEMA_SQL.sessions` 建表语句：`sessions` 62→**65**，
#: 新增的正是这三个 `INTEGER NOT NULL DEFAULT 0`；`messages` 30 列**未变**。
#: 上游这两版怎么落地：v32 只"记账"（列由 `_reconcile_columns()` 的
#: `ALTER TABLE … ADD COLUMN` 从 SCHEMA_SQL 补齐），v33 是纯数据迁移（只 `UPDATE model_config`
#: 补 `_delegate_from`，无 DDL）⇒ 32 与 33 的表形状相同，故两者共用一个增量。
V32_NEW_COLUMNS = [
    ("runtime_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("runtime_generation", "INTEGER NOT NULL DEFAULT 0"),
    ("transcript_revision", "INTEGER NOT NULL DEFAULT 0"),
]


def build_db(path, schema_version=30):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    con.execute(SYSTEM_PROMPTS_DDL)
    con.execute(SESSIONS_DDL)
    con.execute(MESSAGES_DDL)
    for d in EXTRA_DDL:
        con.execute(d)
    if schema_version >= 31:
        for table, cols in V31_NEW_COLUMNS.items():
            for col_name, col_decl in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_decl}")
    if schema_version >= 32:
        for col_name, col_decl in V32_NEW_COLUMNS:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_decl}")
    con.execute("INSERT INTO schema_version (version) VALUES (?)", (schema_version,))
    con.commit()
    con.close()


SRC = "20260914_173913_55daeb"
T = 1789378755.0


def seed_source(path, session_id=SRC, n=6):
    """写入一个"源会话"：n 条消息（user/assistant 交替，末条为 assistant 且有正文）。"""
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO sessions (id, source, started_at, message_count, title, title_source, cwd)"
        " VALUES (?,?,?,?,?,?,?)",
        (session_id, "cli", T, n, "源会话", "derived", "/tmp/hermes-scratch"),
    )
    for i in range(n):
        role = "assistant" if i % 2 else "user"
        content = ("TURN-%d" % i) + (" teal" if role == "assistant" else "")
        # i==2 那一行**自带 BLOB**：真机实测 `display_identity` 是 BLOB
        # ⇒ `read_raw` 必须能序列化 bytes（否则 Object of type bytes is not JSON serializable，
        #    2026-09-15 第一次真机跑就撞上）。
        # ⚠️ 两个前提缺一不可：① 在**被复制的前缀内**（i=2 < cut=4）② **不新增行**
        #    （新增行会打乱 user/assistant 交替，让 `--line 4` 落到 user 上而报错 —— 第一版就这么错的）。
        blob = b"\x00\x01\x02" if i == 2 else None
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active, compacted, display_identity)"
            " VALUES (?,?,?,?,1,0,?)",
            (session_id, role, content, T + i, blob),
        )
    con.commit()
    con.close()


def q(path, sql, args=()):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


print("=== 1. 注册表与可用性 ===")
assert "hermes" in get_adapter.__globals__["_SPECS"], "hermes 应在 _SPECS 里"
os.path.exists(DB) or build_db(DB)
seed_source(DB)
adapter = get_adapter("hermes")
assert adapter.name == "hermes"
assert adapter.is_available() is True
print("✓ adapter 可构造 / is_available / name")

print()
print("=== 2. 版本闸：高危版本必须拒绝写（且 fail-fast，无副作用）===")
adapter_bad = get_adapter("hermes")
os.rename(DB, DB + ".bak")
# 用**紧邻白名单上界**的 34（而不是 99）：白名单是 (30,31,32,33)，"再多一版"正是
# 白名单机制必须继续拦住的那个位置；99 与 34 走同一条判定，测 34 才有意义。
build_db(DB, schema_version=34)
seed_source(DB)
try:
    create_fork(adapter_bad, session_ref=SRC, line_no=4, name="闸门测试", backups_dir=BK)
    raise AssertionError("schema_version=34 时应当拒绝写入")
except Exception as e:
    assert "schema_version" in str(e), f"错误信息应说明版本，实为 {e}"
    print(f"✓ 拒绝写入：{str(e)[:60]}…")
assert q(DB, "SELECT COUNT(*) c FROM sessions")[0]["c"] == 1, "被拒绝时不该新建会话行"
os.remove(DB)
os.rename(DB + ".bak", DB)
adapter = get_adapter("hermes")

print()
print("=== 3. 正常 fork（L1/L2 落库 + 引擎 verify）===")
res = create_fork(adapter, session_ref=SRC, line_no=4, name="验收分支", backups_dir=BK)
assert res.ok, res.error
NEW = res.new_id
print(f"✓ create_fork ok：cut={res.cut} / replacements={res.replacements} / verify={res.verified}")
assert res.verified is True

row = q(DB, "SELECT * FROM sessions WHERE id=?", (NEW,))[0]
assert row["parent_session_id"] == SRC, "血缘列必须指向源会话"
mc = json.loads(row["model_config"])
assert mc == {"_branched_from": SRC}, f"标记必须与产品自身 fork 同形（只有这一个键），实为 {mc}"
assert row["message_count"] == res.cut
assert row["title"] == "验收分支" and row["title_source"] == "user"
assert row["archived"] == 0 and row["hidden"] == 0, "新分支不该继承归档/隐藏态"
assert row["ended_at"] is None and row["end_reason"] is None
print("✓ sessions 行：parent_session_id / _branched_from / message_count / title / 可见性")

msgs = q(DB, "SELECT id, session_id, role, content, display_identity FROM messages WHERE session_id=? ORDER BY id", (NEW,))
assert len(msgs) == res.cut, f"应复制 {res.cut} 条，实为 {len(msgs)}"
assert all(m["session_id"] == NEW for m in msgs), "消息外键必须改写成新 id"
blob = [m for m in msgs if m["display_identity"] is not None]
assert blob, ("前缀内必须含一条 BLOB 行（display_identity）—— 否则 read_raw 的 "
              "bytes→可序列化 回归根本没被覆盖（第一版就踩了这个坑：全绿但零覆盖）")
assert isinstance(blob[0]["display_identity"], (bytes, bytearray)), "BLOB 列写入后应仍是 bytes（不被降级成字符串）"
print(f"✓ messages：{len(msgs)} 条、session_id 全部改写、BLOB 行 {len(blob)} 条（原样保留 bytes）")

src = q(DB, "SELECT COUNT(*) c FROM messages WHERE session_id=?", (SRC,))[0]["c"]
assert src == 6, f"源会话必须零改动（应为 6 条），实为 {src}"
print("✓ 源会话零改动")

lineage = json.load(open(os.path.join(tmpdir, "fork.lineage.json"), encoding="utf-8"))
b = [x for x in lineage["branches"] if x["id"] == NEW]
assert b and b[0]["parent_id"] == SRC and b[0]["at_seq"] == res.cut
print("✓ 旁路谱系索引（parent_id / at_seq）")

errs = verify_branch(adapter, f"{DB}#{NEW}", NEW, res.cut, SRC)
assert errs == [], f"verify_branch 应零错误，实为 {errs}"
print("✓ verify_branch 零错误")

print()
print("=== 3.5 v0.21.6（schema 31）：闸门必须放行，且新增列必须原样复制 ===")
# 出处：2026-10-09 实测 —— 产品 v0.21.6 把 schema_version 升到 31 后，
# 旧的"等值闸"（只认 30）会**直接拒绝写入**（真机上用户就是这样被拦下的）。
# 本条固化两件事：① 31 在白名单里、写路径放行；② v31 新增的 8 个列随行原样复制。
os.rename(DB, DB + ".bak31")
build_db(DB, schema_version=31)
seed_source(DB)
con = sqlite3.connect(DB)
# 模拟"由产品写出的" v31 取值（真机迁移后这些列就长这样）
con.execute("UPDATE messages SET message_uid='uid-' || id WHERE session_id=?", (SRC,))
# 顺带把源会话设成"已归档 + 自动归档"，验证分支**两个归档位都会被归零**
# （隐患：只清 archived 会留下 `archived=0 且 auto_archived=1` 这种产品语义下不该存在的组合）
con.execute("UPDATE sessions SET created_source='cli', transport_profile='local',"
            " archived=1, auto_archived=1 WHERE id=?", (SRC,))
con.commit()
con.close()

adapter31 = get_adapter("hermes")
res31 = create_fork(adapter31, session_ref=SRC, line_no=4, name="v31 验收分支", backups_dir=BK)
assert res31.ok, res31.error
NEW31 = res31.new_id
print(f"✓ v31 写入放行：cut={res31.cut} / replacements={res31.replacements} / verify={res31.verified}")
assert res31.verified is True

src_uids = [r["message_uid"] for r in
            q(DB, "SELECT message_uid FROM messages WHERE session_id=? ORDER BY id", (SRC,))][:res31.cut]
new_uids = [r["message_uid"] for r in
            q(DB, "SELECT message_uid FROM messages WHERE session_id=? ORDER BY id", (NEW31,))]
assert new_uids == src_uids, f"v31 新增列必须原样复制：{new_uids} != {src_uids}"
print(f"✓ messages.message_uid 原样复制（同序同值）：{new_uids}")

row31 = q(DB, "SELECT created_source, transport_profile, compression_overload_streak, auto_archived,"
              " archived, hidden FROM sessions WHERE id=?", (NEW31,))[0]
assert row31["created_source"] == "cli" and row31["transport_profile"] == "local", \
    f"v31 sessions 新增列未原样复制：{dict(row31)}"
assert row31["archived"] == 0 and row31["hidden"] == 0 and row31["auto_archived"] == 0, \
    f"分支不得继承任何归档/隐藏态（含 v31 的 auto_archived）：{dict(row31)}"
print(f"✓ sessions 新列随行复制：created_source={row31['created_source']} / "
      f"transport_profile={row31['transport_profile']} / "
      f"streak={row31['compression_overload_streak']}；"
      f"归档位全部归零：archived={row31['archived']} hidden={row31['hidden']} "
      f"auto_archived={row31['auto_archived']}（源会话是 archived=1/auto_archived=1）")

os.remove(DB)
os.rename(DB + ".bak31", DB)

print()
print("=== 3.6 v0.23.1（schema 33）：闸门放行，且三个计数器必须归零（不继承源行）===")
# 出处：上游 tag v0.23.1（commit 3b42157）—— `SCHEMA_VERSION = 33`、`sessions` 65 列。
# 真机上"只认 30/31"的白名单会把用户直接拦下（拒绝本身是保护，但对用户是"分支打不出来"）。
# 本条固化三件事：① 33 在白名单里、写路径放行；② fixture 的表形状与上游记录一致（65 / 30）；
# ③ 三个新计数器取 0（产品自己发布子行的 SQL 里这三列不在列清单 ⇒ 子行取表默认值 0）。
os.rename(DB, DB + ".bak33")
build_db(DB, schema_version=33)
seed_source(DB)

# 表形状断言：锁住**我们声称的**形状（65 / 30），防止 DDL 增量和上游记录脱节
# （例如有人改了 V31/V32_NEW_COLUMNS 却忘了列数会变）。它证明的是 fixture 的忠实度，
# 不是上游的真实列数 —— 后者的判据记在上面 V32_NEW_COLUMNS 的注释里。
_shape_con = sqlite3.connect(DB)
try:
    n_sess_cols = len(_shape_con.execute("SELECT * FROM sessions LIMIT 0").description)
    n_msg_cols = len(_shape_con.execute("SELECT * FROM messages LIMIT 0").description)
finally:
    _shape_con.close()
assert (n_sess_cols, n_msg_cols) == (65, 30), \
    f"synthetic v33 的表形状应与上游一致（sessions 65 / messages 30），实为 {n_sess_cols}/{n_msg_cols}"
print(f"✓ 表形状：sessions {n_sess_cols} 列 / messages {n_msg_cols} 列（与上游 v0.23.1 的 SCHEMA_SQL 一致）")

con = sqlite3.connect(DB)
# 模拟"被产品跑过很多轮"的源会话：三个计数器都非 0，且已归档 + 自动归档。
# 非 0 是**必须的**——否则"分支归零"和"分支继承"两种实现的断言结果相同（零覆盖）。
con.execute("UPDATE sessions SET runtime_revision=41, runtime_generation=7, transcript_revision=93,"
            " created_source='cli', transport_profile='local', archived=1, auto_archived=1"
            " WHERE id=?", (SRC,))
con.execute("UPDATE messages SET message_uid='uid-' || id WHERE session_id=?", (SRC,))
con.commit()
con.close()
src_row = q(DB, "SELECT runtime_revision, runtime_generation, transcript_revision FROM sessions WHERE id=?",
            (SRC,))[0]
assert (src_row["runtime_revision"], src_row["runtime_generation"], src_row["transcript_revision"]) == (41, 7, 93)

adapter33 = get_adapter("hermes")
res33 = create_fork(adapter33, session_ref=SRC, line_no=4, name="v33 验收分支", backups_dir=BK)
assert res33.ok, res33.error
NEW33 = res33.new_id
print(f"✓ v33 写入放行：cut={res33.cut} / replacements={res33.replacements} / verify={res33.verified}")
assert res33.verified is True

row33 = q(DB, "SELECT * FROM sessions WHERE id=?", (NEW33,))[0]
# 血缘：与产品自身 fork 同形（两列配合）
assert row33["parent_session_id"] == SRC, "血缘列必须指向源会话"
assert json.loads(row33["model_config"]) == {"_branched_from": SRC}, \
    f"标记必须与产品自身 fork 同形（只有这一个键），实为 {row33['model_config']}"
assert row33["message_count"] == res33.cut
assert row33["title"] == "v33 验收分支" and row33["title_source"] == "user"
assert row33["ended_at"] is None and row33["end_reason"] is None
print(f"✓ sessions 行：parent_session_id=源 / _branched_from=源 / message_count={row33['message_count']} / "
      f"title={row33['title']!r} / ended_at=None")

# 三个新计数器必须归零 —— 判据是产品自己发布子行的 INSERT 不含这三列（取表默认值 0）。
assert (row33["runtime_revision"], row33["runtime_generation"], row33["transcript_revision"]) == (0, 0, 0), \
    ("v0.23.1 新增的三个计数器不得继承源行（源为 41/7/93）："
     f"runtime_revision={row33['runtime_revision']} "
     f"runtime_generation={row33['runtime_generation']} "
     f"transcript_revision={row33['transcript_revision']}")
print("✓ 三个新计数器归零（源行是 runtime_revision=41 / runtime_generation=7 / "
      "transcript_revision=93，未继承）")

# 归档位（含 v31 的 auto_archived）同样归零
assert (row33["archived"], row33["hidden"], row33["auto_archived"]) == (0, 0, 0), \
    f"分支不得继承归档/隐藏态：archived={row33['archived']} hidden={row33['hidden']} " \
    f"auto_archived={row33['auto_archived']}"
# 其余 v31 新列仍然随行复制（动态取列的既有性质不能被本次改动破坏）
assert row33["created_source"] == "cli" and row33["transport_profile"] == "local", \
    f"v31 列入仍应原样复制：created_source={row33['created_source']} " \
    f"transport_profile={row33['transport_profile']}"
print(f"✓ 归档位归零：archived=0 hidden=0 auto_archived=0；"
      f"其余新列仍原样复制：created_source={row33['created_source']} "
      f"transport_profile={row33['transport_profile']}")

# 消息侧：30 列未变 ⇒ 复制行为应与 v31 一致（含 BLOB 原样、session_id 全改写）
msgs33 = q(DB, "SELECT id, session_id, content, display_identity FROM messages WHERE session_id=? ORDER BY id",
           (NEW33,))
assert len(msgs33) == res33.cut, f"应复制 {res33.cut} 条，实为 {len(msgs33)}"
assert all(m["session_id"] == NEW33 for m in msgs33), "消息外键必须改写成新 id"
assert any(isinstance(m["display_identity"], (bytes, bytearray)) for m in msgs33), \
    "前缀内的 BLOB 行（display_identity）必须仍是 bytes"
src_cnt = q(DB, "SELECT COUNT(*) c FROM messages WHERE session_id=?", (SRC,))[0]["c"]
assert src_cnt == 6, f"源会话必须零改动（应为 6 条），实为 {src_cnt}"
print(f"✓ messages：{len(msgs33)} 条、session_id 全部改写、BLOB 仍为 bytes、源会话零改动")

errs33 = verify_branch(adapter33, f"{DB}#{NEW33}", NEW33, res33.cut, SRC)
assert errs33 == [], f"v33 上 verify_branch 应零错误，实为 {errs33}"
print("✓ verify_branch 零错误")

os.remove(DB)
os.rename(DB + ".bak33", DB)

print()
print("=== 4. 标题全局唯一（撞名必须让路，不能 IntegrityError 打死整个事务）===")
res2 = create_fork(adapter, session_ref=SRC, line_no=4, name="验收分支", backups_dir=BK)
assert res2.ok, res2.error
titles = sorted(r["title"] for r in q(DB, "SELECT title FROM sessions WHERE title LIKE '验收分支%'"))
assert titles == ["验收分支", "验收分支 (2)"], f"应唯一化，实为 {titles}"
print(f"✓ 撞名后标题：{titles}")

print()
print("=== 5. 无标记 ⇒ 产品看不见（本适配器必写标记的直接理由）===")
con = sqlite3.connect(DB)
con.execute("UPDATE sessions SET model_config='{}' WHERE id=?", (NEW,))
con.commit()
con.close()
assert q(DB, "SELECT model_config FROM sessions WHERE id=?", (NEW,))[0]["model_config"] == "{}"
print("✓ 已构造无标记样本（真机对照：产品不列出该会话）")

print()
print("✅ Hermes 适配器契约测试全部通过")
import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

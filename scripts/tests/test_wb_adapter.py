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

# 造源会话行（schema 完全对齐真库 ~/.workbuddy/workbuddy.db sessions 表）
conn = sqlite3.connect(db_path)
conn.execute(
    """CREATE TABLE sessions (
        id TEXT, cwd TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT,
        custom_title TEXT, status TEXT NOT NULL DEFAULT 'Pending',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
        is_playground INTEGER NOT NULL DEFAULT 0, source_mode TEXT,
        is_background_automation INTEGER, mode TEXT, model TEXT, expert_id TEXT,
        expert_locale TEXT, expert_runtime_identity TEXT, expert_marketplace TEXT,
        permission_mode TEXT, last_activity_at INTEGER, use_sandbox_cli INTEGER,
        project_id TEXT, plugin_context_json TEXT,
        last_user_prompt_expert_selection TEXT, context_window INTEGER,
        thought_level TEXT, addon_selection TEXT, session_settings TEXT)"""
)
src_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
now = 1786000000000
conn.execute(
    "INSERT INTO sessions (id, cwd, user_id, title, custom_title, status, created_at, updated_at, is_playground) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    (src_id, "/tmp/test", "test-user", "测试源会话标题", "测试源会话", "working", now, now, 0),
)
conn.commit()
conn.close()

# 造 transcript 文件（含真实 WorkBuddy 字段结构：tool 消息的 output/arguments/
# argumentsDisplayText/renderer.value/error.message —— 正是白名单替换漏掉的字段）
lines = [
    {"type": "message", "role": "user", "sessionId": src_id,
     "content": [{"type": "input_text", "text": "你好"}]},
    {"type": "function_call", "sessionId": src_id, "name": "bash",
     "arguments": f'{{"command": "echo {src_id}"}}',
     "output": {"text": f"输出里引用了 {src_id}"},
     "providerData": {
         "argumentsDisplayText": f"echo {src_id}",
         "toolResult": {
             "content": f"结果 {src_id}",
             "renderer": {"type": "text", "value": f"渲染 {src_id}"},
         },
     }},
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

# ============================================================
# 10. rewrite_ids 专项：覆盖全部可读字段 + rawContent 黑名单
# ============================================================
import copy

adapter2 = WorkBuddyAdapter()  # 不依赖 db，纯函数级测试
oid, nid = "old-1111-2222-3333-4444", "new-aaaa-bbbb-cccc-dddd"
sample = [
    {
        "sessionId": oid, "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": f"引用 {oid} 在正文"}],
        "output": {"text": f"output 里的 {oid}"},
        "arguments": f'{{"sessionId": "{oid}"}}',
        "providerData": {
            "argumentsDisplayText": f"node x.mjs {oid}",
            "error": {"code": 1, "message": f"错误 {oid}"},
            "toolResult": {"content": f"结果 {oid}", "renderer": {"type": "x", "value": f"渲染 {oid}"}},
            "rawResponse": f"原始响应 {oid}（不应改写）",
        },
    },
    {"sessionId": oid, "type": "file-history-snapshot", "rawContent": f"原始内容 {oid}（不应改写）"},
]
out, n = adapter2.rewrite_ids(copy.deepcopy(sample), oid, nid)
s = out[0]
assert s["sessionId"] == nid, "sessionId 未替换"
assert oid not in s["content"][0]["text"] and nid in s["content"][0]["text"], "content.text 未替换"
assert oid not in s["output"]["text"], "output.text 未替换"
assert oid not in s["arguments"], "顶层 arguments 未替换"
assert oid not in s["providerData"]["argumentsDisplayText"], "argumentsDisplayText 未替换"
assert oid not in s["providerData"]["error"]["message"], "error.message 未替换"
assert oid not in s["providerData"]["toolResult"]["content"], "toolResult.content 未替换"
assert oid not in s["providerData"]["toolResult"]["renderer"]["value"], "renderer.value 未替换"
assert oid in s["providerData"]["rawResponse"], "rawResponse 被改写（违反安全承诺）"
assert oid in out[1]["rawContent"], "rawContent 被改写（违反安全承诺）"
print(f"✓ rewrite_ids 专项: {n} 处替换，rawContent/rawResponse 黑名单生效")

# ============================================================
# 11. fork --verify 真库体检：临时库有真实会话 → L2 全绿
# ============================================================
from fork_core.engine import verify_environment

items = verify_environment(adapter)
fails = [it for it in items if not it.ok]
real_replace = [it for it in items if it.name.startswith("真实数据替换")]
assert not fails, f"verify 有失败项: {[(i.name, i.detail) for i in fails]}"
assert real_replace, "verify 未跑真实数据替换验证"
assert all(it.level == "L2" for it in real_replace), "真实数据替换应为 L2"
print(f"✓ fork --verify: {len(items)} 项全绿，真实数据替换 L2（{len(real_replace)} 个会话）")

import shutil
shutil.rmtree(tmpdir)

# ============================================================
# 12. 原子性：verify 失败不留半成品（学习 Marvis 事务内自检，v2.4.2）
#     verify 前置到 register 之前；失败删文件，无 db/lineage 痕迹
# ============================================================
import tempfile as _tf
import fork_core.engine as _eng

_atom_tmp = _tf.mkdtemp(prefix="wb-atom-")
import fork_core.adapters.workbuddy as wb_mod2
wb_mod2.DB_PATH = os.path.join(_atom_tmp, "workbuddy.db")
wb_mod2.PROJECTS_DIR = os.path.join(_atom_tmp, "projects")
wb_mod2.LINEAGE_PATH = os.path.join(_atom_tmp, "fork.lineage.json")
os.makedirs(os.path.join(wb_mod2.PROJECTS_DIR, "Users-x-test"))
_c = sqlite3.connect(wb_mod2.DB_PATH)
_c.execute("""CREATE TABLE sessions (id TEXT, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
    title TEXT, custom_title TEXT, status TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
    is_playground INTEGER NOT NULL DEFAULT 0, last_activity_at INTEGER)""")
_c.execute("INSERT INTO sessions (id,cwd,user_id,status,created_at,updated_at,is_playground) VALUES (?,?,?,?,?,?,?)",
           ("SRC-1111-2222-3333-4444","/tmp","u","working",1,1,0))
_c.commit(); _c.close()
_src = "SRC-1111-2222-3333-4444"
with open(os.path.join(wb_mod2.PROJECTS_DIR, "Users-x-test", _src+".jsonl"), "w") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_src,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_src,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_src,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")

_orig_vb = _eng.verify_branch
_eng.verify_branch = lambda *a, **k: ["forced failure"]
from fork_core.engine import create_fork as _cf
from fork_core.adapters.workbuddy import WorkBuddyAdapter as _WBA
_atom_a = _WBA()
_try_fail = False
try:
    _cf(_atom_a, _src, name="T", dry_run=False)
except _eng.ForkError:
    _try_fail = True
_eng.verify_branch = _orig_vb
assert _try_fail, "verify 失败应 raise"
_c2 = sqlite3.connect(wb_mod2.DB_PATH)
_n = _c2.execute("SELECT count(*) FROM sessions").fetchone()[0]
_c2.close()
_files = os.listdir(os.path.join(wb_mod2.PROJECTS_DIR, "Users-x-test"))
_lin = json.load(open(wb_mod2.LINEAGE_PATH)) if os.path.exists(wb_mod2.LINEAGE_PATH) else {"forks": []}
assert _n == 1 and len(_files) == 1 and not _lin.get("forks"), "verify 失败应不留痕迹"
shutil.rmtree(_atom_tmp)
print("✓ 原子性: verify 失败不留半成品（db/文件/lineage 全干净）")
# ============================================================
# 14. register 失败回滚方向（撤 db 副作用，保留合格文件）v2.4.3
# ============================================================
import fork_core.adapters.workbuddy as _wb4
_rb_tmp = _tf.mkdtemp(prefix="wb-roll-")
_wb4.DB_PATH = os.path.join(_rb_tmp, "workbuddy.db")
_wb4.PROJECTS_DIR = os.path.join(_rb_tmp, "projects")
_wb4.LINEAGE_PATH = os.path.join(_rb_tmp, "fork.lineage.json")
os.makedirs(os.path.join(_wb4.PROJECTS_DIR, "Users-x-test"))
_rc = sqlite3.connect(_wb4.DB_PATH)
_rc.execute("""CREATE TABLE sessions (id TEXT, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
    title TEXT, custom_title TEXT, status TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
    is_playground INTEGER NOT NULL DEFAULT 0, last_activity_at INTEGER)""")
_rc.execute("INSERT INTO sessions (id,cwd,user_id,status,created_at,updated_at,is_playground) VALUES (?,?,?,?,?,?,?)",
            ("SRC-1111-2222-3333-4444","/tmp","u","working",1,1,0))
_rc.commit(); _rc.close()
_rsrc = "SRC-1111-2222-3333-4444"
with open(os.path.join(_wb4.PROJECTS_DIR, "Users-x-test", _rsrc+".jsonl"), "w") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_rsrc,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_rsrc,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_rsrc,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")
from fork_core.adapters.workbuddy import WorkBuddyAdapter as _WBA3
_orig_reg2 = _WBA3.register_branch
def _fake_reg2(self, src, new_id, dst_path, name, parent_id=None, at_seq=None):
    db = self._connect()
    db.execute("INSERT INTO sessions (id,cwd,user_id,status,created_at,updated_at,is_playground) VALUES (?,?,?,?,?,?,?)",
               (new_id,"/tmp","u","terminated",1,1,0))
    db.commit(); db.close()
    raise RuntimeError("simulated lineage failure")
_WBA3.register_branch = _fake_reg2
_rfailed = False
try:
    _cf(_WBA3(), _rsrc, name="T", dry_run=False)
except _eng.ForkError:
    _rfailed = True
_WBA3.register_branch = _orig_reg2
assert _rfailed, "register 失败应抛 ForkError"
_rc2 = sqlite3.connect(_wb4.DB_PATH)
_rrows = _rc2.execute("SELECT id FROM sessions").fetchall()
_rc2.close()
_rfiles = os.listdir(os.path.join(_wb4.PROJECTS_DIR, "Users-x-test"))
assert len(_rrows) == 1, "unregister 应清掉 db 残留行"
assert len(_rfiles) == 1, "L0: register 失败应回滚文件（干净回退，无孤儿无悬空）"
shutil.rmtree(_rb_tmp)
print("✓ 回滚方向: register 失败撤 db/谱系 + 回滚文件（干净回退，可安全重跑）")

# ============================================================
# 15. L0 事务化（焊死顺序 + dry-run 校验 + rollback 显式报错）v2.4.3
# ============================================================
import fork_core.adapters.workbuddy as _wb5
_l0_tmp = _tf.mkdtemp(prefix="wb-l0-")
_wb5.DB_PATH = os.path.join(_l0_tmp, "workbuddy.db")
_wb5.PROJECTS_DIR = os.path.join(_l0_tmp, "projects")
_wb5.LINEAGE_PATH = os.path.join(_l0_tmp, "fork.lineage.json")
os.makedirs(os.path.join(_wb5.PROJECTS_DIR, "Users-x-test"))
_lc = sqlite3.connect(_wb5.DB_PATH)
_lc.execute("""CREATE TABLE sessions (id TEXT, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
    title TEXT, custom_title TEXT, status TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
    is_playground INTEGER NOT NULL DEFAULT 0, last_activity_at INTEGER)""")
_lc.execute("INSERT INTO sessions (id,cwd,user_id,status,created_at,updated_at,is_playground) VALUES (?,?,?,?,?,?,?)",
            ("SRC-1111-2222-3333-4444","/tmp","u","working",1,1,0))
_lc.commit(); _lc.close()
_lsrc = "SRC-1111-2222-3333-4444"
with open(os.path.join(_wb5.PROJECTS_DIR, "Users-x-test", _lsrc+".jsonl"), "w") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_lsrc,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_lsrc,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_lsrc,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")
from fork_core.adapters.workbuddy import WorkBuddyAdapter as _WBA4
from fork_core.engine import ForkVerifyError as _FVE, ForkRollbackError as _FRE

# 15a. 焊死顺序：verify 失败时 register 零调用
_reg_calls = []
_oreg = _WBA4.register_branch
_WBA4.register_branch = lambda *a, **k: _reg_calls.append(1) or _oreg(*a, **k)  # 记录调用并照常执行
_owb = _WBA4.write_branch
_WBA4.write_branch = lambda self, p, t: _owb(self, p, t + [{"sessionId": "OLD-SRC-STILL-HERE"}])  # 造 verify 必失败的坏内容
_ovb = _eng.verify_branch
_try_verify_fail = False
try:
    _cf(_WBA4(), _lsrc, name="T", dry_run=False)
except _FVE:
    _try_verify_fail = True
finally:
    _WBA4.write_branch = _owb
    _WBA4.register_branch = _oreg
    _eng.verify_branch = _ovb
assert _try_verify_fail, "坏内容应触发 ForkVerifyError"
# 断言 verify 失败时 register 从未被调用（顺序焊死）
assert _reg_calls == [], "verify 失败时 register 不应被调用"
# 断言零痕迹：无 dst 文件、无 tmp 残留、db 只有源
_lf = os.listdir(os.path.join(_wb5.PROJECTS_DIR, "Users-x-test"))
assert all(".tmp" not in f for f in _lf), "tmp 不应残留"
assert len([f for f in _lf if f != _lsrc+".jsonl"]) == 0, "不应有分支文件"
print("✓ 顺序焊死: verify 失败时 register 零调用 + tmp/dst 零残留")

# 15b. dry-run 走完整校验（坏内容时 dry-run 也要报 ForkVerifyError）
_dry_fail = False
_owb2 = _WBA4.write_branch
_WBA4.write_branch = lambda self, p, t: _owb2(self, p, t + [{"sessionId": "OLD-SRC"}])
try:
    _cf(_WBA4(), _lsrc, name="T", dry_run=True)
except _FVE:
    _dry_fail = True
finally:
    _WBA4.write_branch = _owb2
assert _dry_fail, "dry-run 遇到坏内容应报 ForkVerifyError（洞 5：dry-run 也要校验）"
# dry-run 正常内容应 verified=True
_rv = _cf(_WBA4(), _lsrc, name="T", dry_run=True)
assert _rv.verified, "dry-run 应返回 verified=True"
assert not os.path.exists(_rv.dst_path), "dry-run 不应落位正式文件"
print("✓ dry-run 校验: 正常内容 verified=True，坏内容抛 ForkVerifyError，不落位")

# 15c. rollback 失败显式报错（safe_remove 返回 False → ForkRollbackError 带路径）
import fork_core.engine as _eng2
_orig_sr = _eng2._safe_remove
def _fake_sr(path):
    if path.endswith(".jsonl") and ".tmp" not in path:
        return False  # dst 删不掉
    return _orig_sr(path)
_eng2._safe_remove = _fake_sr
_reg_throw = False
_owb3 = _WBA4.register_branch
_WBA4.register_branch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
try:
    try:
        _cf(_WBA4(), _lsrc, name="T", dry_run=False)
    except _FRE as e:
        _reg_throw = True
        assert _lsrc or True
finally:
    _WBA4.register_branch = _owb3
    _eng2._safe_remove = _orig_sr
assert _reg_throw, "回滚失败应抛 ForkRollbackError（不静默）"
print("✓ rollback 显式报错: 删不掉抛 ForkRollbackError（含人工清理提示）")
shutil.rmtree(_l0_tmp)



print("\n✅ WorkBuddy adapter 全部测试通过（含谱系/再 fork/谱系树/rewrite 专项/真库体检/原子性）")

# ============================================================
# 13. dry_run 不崩溃（回归：rewrite 移入 if 后 ForkResult 引用局部名）v2.4.3
# ============================================================
_dr_tmp = _tf.mkdtemp(prefix="wb-dry-")
import importlib, fork_core.adapters.workbuddy as _wb3
_wb3.DB_PATH = os.path.join(_dr_tmp, "workbuddy.db")
_wb3.PROJECTS_DIR = os.path.join(_dr_tmp, "projects")
_wb3.LINEAGE_PATH = os.path.join(_dr_tmp, "fork.lineage.json")
os.makedirs(os.path.join(_wb3.PROJECTS_DIR, "Users-x-test"))
_dc = sqlite3.connect(_wb3.DB_PATH)
_dc.execute("""CREATE TABLE sessions (id TEXT, cwd TEXT NOT NULL, user_id TEXT NOT NULL,
    title TEXT, custom_title TEXT, status TEXT NOT NULL DEFAULT 'Pending',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
    is_playground INTEGER NOT NULL DEFAULT 0, last_activity_at INTEGER)""")
_dc.execute("INSERT INTO sessions (id,cwd,user_id,status,created_at,updated_at,is_playground) VALUES (?,?,?,?,?,?,?)",
            ("SRC-1111-2222-3333-4444","/tmp","u","working",1,1,0))
_dc.commit(); _dc.close()
_dsrc = "SRC-1111-2222-3333-4444"
with open(os.path.join(_wb3.PROJECTS_DIR, "Users-x-test", _dsrc+".jsonl"), "w") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_dsrc,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_dsrc,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_dsrc,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")
from fork_core.adapters.workbuddy import WorkBuddyAdapter as _WBA2
_dr = _cf(_WBA2(), _dsrc, name="T", dry_run=True)  # 不应 UnboundLocalError
assert _dr.ok and _dr.verified, "dry-run 应 ok 且 verified"
assert _dr.cut == 2, "dry-run 应报截断点"
print(f"✓ dry_run 正常（截断 L{_dr.cut}，不崩溃）")
shutil.rmtree(_dr_tmp)
print("\n✅ WorkBuddy adapter 全部测试通过（含 dry_run/原子性/回滚/真库体检）")

import json
import os
import sqlite3
import sys
import tempfile

# 自定位：把技能根目录（本文件的上一级）加入模块搜索路径，任何机器都能直接跑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fork_core.adapter_workbuddy as wb_mod
from fork_core.adapter_workbuddy import WorkBuddyAdapter

# --- 临时 db + 临时 projects ---
tmpdir = tempfile.mkdtemp(prefix="wb-test-")
db_path = os.path.join(tmpdir, "workbuddy.db")
proj_dir = os.path.join(tmpdir, "projects", "test-workspace")
os.makedirs(proj_dir, exist_ok=True)

wb_mod.DB_PATH = db_path
wb_mod.PROJECTS_DIR = os.path.join(tmpdir, "projects")
# 谱系索引也必须隔离：否则测试造的分支会被写进**生产** ~/.workbuddy/fork.lineage.json
# （2026-09-14 修复——此前测试跑一次就往真实谱系塞 2 条 cwd=/tmp/test 的假分支）
wb_mod.LINEAGE_PATH = os.path.join(tmpdir, "fork.lineage.json")

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
with open(os.path.join(proj_dir, src_id + ".jsonl"), "w", encoding="utf-8") as f:
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

# ============================================================
# 11b. 分支校验边界回归（v2.4.4）：
#      fork 完成后产品会继续往分支文件追加消息；追加内容里可能恰好出现源会话 id
#      （真实案例：追加的 `ls ~/.workbuddy/tasks` 输出里有个目录叫源会话 UUID）。
#      旧实现用"当前最后一轮"当边界 → 把追加内容算进来 → 误报"源 id 残留"。
#      正确边界 = 谱系记录的快照点 at_seq（= fork 复制出的前缀行数）。
# ============================================================
from fork_core.engine import _find_residue, _load_lines, locate_last_reply

appended = [
    # ① 合法追加必然开启新回合：首行是 user 消息
    #    （两个真实分支实测：L(at_seq+1) 都是 user 消息）
    {"type": "message", "role": "user", "sessionId": r.new_id,
     "content": [{"type": "input_text", "text": "继续"}]},
    # ② 追加的 tool 输出里恰好含源会话 id（这正是真实误报的形态）
    {"type": "function_call", "sessionId": r.new_id, "name": "bash",
     "output": {"text": f"=== 最近的任务 uuid ===\n{src_id}\n"}},
    # ③ 末尾再来一条 assistant 回复，使"当前最后一轮"落到毒行之后
    {"type": "message", "role": "assistant", "sessionId": r.new_id,
     "content": [{"type": "output_text", "text": "已列出。"}]},
]
with open(r.dst_path, "a", encoding="utf-8") as f:
    for o in appended:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")

items_b = verify_environment(adapter)
_br = [it for it in items_b if it.name.startswith("分支产物校验")]
assert _br, "分支产物校验项缺失"
_bad = [it for it in _br if not it.ok]
assert not _bad, f"fork 后追加内容被误判为源 id 残留: {[(i.name, i.detail) for i in _bad]}"

# 负向验证：旧边界（当前最后一轮）在同一数据上必然误报 → 证明本用例有真实拦截力
_lines_now = _load_lines(r.dst_path)
_old_cut, _ = locate_last_reply(adapter, _lines_now)
_old_residue = _find_residue(_lines_now[:_old_cut], src_id, getattr(adapter, "_RAW_KEYS", set()))
assert _old_residue, "负向验证失败：旧边界逻辑本应误报，说明用例没打到点上"

print(f"✓ 分支校验边界: fork 后追加 {len(appended)} 行不计入"
      f"（{len(_br)} 项全绿；负向验证：旧边界会误报 {len(_old_residue)} 处）")

# ============================================================
# 11c. 边界可信度兜底（v2.4.4，复核发现的"漏检比误报危险"）：
#      只查 at_seq 前缀 → 若 at_seq 偏小会**静默漏检**。
#      两道兜底：① at_seq 不可信则退回更宽边界（只会误报，不会漏检）；
#                ② 全文件 sessionId 结构性检查（不依赖边界，永远硬拦）。
# ============================================================
_branch_item_name = f"分支产物校验 {r.new_id[:8]}"

def _set_at_seq(v):
    d = json.load(open(wb_mod.LINEAGE_PATH, encoding="utf-8"))
    for f in d["forks"]:
        if f["id"] == r.new_id:
            f["at_seq"] = v
    with open(wb_mod.LINEAGE_PATH, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)

def _item():
    hit = [it for it in verify_environment(adapter) if it.name == _branch_item_name]
    assert hit, f"未找到 {_branch_item_name}"
    return hit[0]

# 兜底①：把 at_seq 改成不可信值（1 是 user 消息，不可能是"完整 assistant 回复"收尾的前缀）
_set_at_seq(1)
_it = _item()
assert not _it.ok, f"at_seq 不可信时应退回宽边界并抓到残留，而不是静默通过：{_it.detail}"

# 兜底②：恢复正确快照点，再追加一条 sessionId 直接等于源 id 的记录
#        （真污染：产品会按 sessionId 把它路由回源会话）——位于快照点之后，边界查不到
_set_at_seq(r.cut)
with open(r.dst_path, "a", encoding="utf-8") as f:
    f.write(json.dumps({
        "type": "message", "role": "assistant", "sessionId": src_id,
        "content": [{"type": "output_text", "text": "结构性污染"}],
    }, ensure_ascii=False) + "\n")
_it = _item()
assert not _it.ok, f"结构性 sessionId 残留必须被抓到：{_it.detail}"
assert "sessionId" in _it.detail, f"应指出是结构性残留：{_it.detail}"
print(f"✓ 边界可信度兜底: at_seq 不可信 → 退回宽边界抓到残留；"
      f"快照点后的 sessionId 结构性残留 → 不依赖边界硬拦")

# ============================================================
# 11d. v2.4.15 判据变更：边界之外的源 id 引用 → **需复核**，不再判失败
#
#      这条**替换**了原先的断言（原文：「边界跳过的正文残留必须被抓到（反例场景）」）。
#      为什么改（2026-09-19 结论，不是为了让测试变绿）：
#        · 原判据要拦的是"at_seq 被记小 ⇒ 被跳过的区域其实还是复制内容"。但它在字节上
#          **无法与"在分支里记录血缘"区分**——两者都是"边界之后、正文里出现源 id"。
#          真机取证：两处常驻红的实际内容是"读谱系文件的输出"与"身份登记表 + 自报正文"，
#          主题全是"我从哪来"（而记录血缘本就是分支的常见用法）。
#        · 旧判据的实际效果 = 每正常用一次分支就常驻一条红 ⇒ 闸门变成"狼来了"，
#          真问题被淹没（而 --verify 的全部价值就是发布前拦截）。
#        · "at_seq 记小"在当前代码里发生不了：create 期 `verify_branch` 硬断言
#          "产物行数 == cut"，不等即拒绝落位（见 engine.create_fork 的 VERIFY 段）。
#      仍然硬拦的形态由本用例下半段的负向控制逐条验证（前缀内残留 / 结构性残留）。
# ============================================================
_clean = _load_lines(r.dst_path)[:r.cut]  # 回到"刚 fork 完"的干净前缀


def _rewrite_branch(prefix, extra):
    """用给定前缀 + 追加行重写分支文件。

    注：这里用普通 json.dumps 逐行还原——adapter 的 dumps_safe 默认就是
    `json.dumps(o, ensure_ascii=False)`（仅当对象含孤立代理才降级），故字节可完全复现，
    前缀指纹才会判"一致"。若将来 dumps_safe 改了序列化形态，本用例会先红（这是好事）。
    """
    with open(r.dst_path, "w", encoding="utf-8") as f:
        for _o in list(prefix) + list(extra):
            f.write(json.dumps(_o, ensure_ascii=False) + "\n")


_poison = {
    "type": "message", "role": "assistant", "sessionId": r.new_id,
    "content": [{"type": "output_text", "text": f"正文提到 {src_id} 这个会话"}],
}
_rewrite_branch(_clean, [
    {"type": "message", "role": "user", "sessionId": r.new_id,
     "content": [{"type": "input_text", "text": "继续"}]},
    _poison,
    {"type": "message", "role": "assistant", "sessionId": r.new_id,
     "content": [{"type": "output_text", "text": "收尾"}]},
])

# ── ① 污染在**边界之外** ⇒ 需复核（不判失败），且必须在输出里显式可见 ──
_set_at_seq(r.cut)  # 边界正好落在干净前缀末尾 → 污染行属"fork 之后追加"
_it = _item()
assert _it.ok, f"边界之外的正文引用不该判失败（v2.4.15 判据）：{_it.detail}"
assert getattr(_it, "review", False), f"应标记为需复核：{_it.detail}"
assert "需复核" in _it.detail, f"detail 应写清需复核：{_it.detail}"
assert "前缀指纹与 fork 时一致" in _it.detail, f"应给出边界证据（指纹）：{_it.detail}"

# ── ② 负向控制：同一处污染落在**边界之内** ⇒ 仍硬失败（前缀是引擎的产物）──
_set_at_seq(r.cut + 2)  # 边界扩到污染行末尾（该行是完整 assistant 回复）
_it = _item()
assert not _it.ok, f"前缀内的源 id 残留必须仍判失败：{_it.detail}"
assert "前缀" in _it.detail, f"应指向前缀残留：{_it.detail}"

# ── ③ 负向控制：前缀被改动（指纹漂移）⇒ 据实报出来，但不判失败 ──
_set_at_seq(r.cut)
_drift = json.loads(json.dumps(_clean, ensure_ascii=False))  # 深拷贝，别污染 _clean
for _o in _drift:                                            # 改前缀中段某行的正文（不动任何 id）
    _c = _o.get("content")
    if isinstance(_c, list) and _c and isinstance(_c[0], dict) and "text" in _c[0]:
        _c[0]["text"] = str(_c[0]["text"]) + "（事后改动）"
        break
_rewrite_branch(_drift, [
    {"type": "message", "role": "user", "sessionId": r.new_id,
     "content": [{"type": "input_text", "text": "继续"}]},
    {"type": "message", "role": "assistant", "sessionId": r.new_id,
     "content": [{"type": "output_text", "text": "收尾"}]},
])
_it = _item()
assert _it.ok, f"指纹漂移不该判失败（前缀内无源 id 残留）：{_it.detail}"
assert getattr(_it, "review", False), f"指纹漂移应标记需复核：{_it.detail}"
assert "指纹与 fork 时不符" in _it.detail, f"应报出前缀被改动：{_it.detail}"

print("✓ 边界判据（v2.4.15）：边界外引用 → 需复核（含指纹漂移）；"
      "前缀内残留 → 仍硬失败（负向控制 2 条）")

import shutil
shutil.rmtree(tmpdir)

# ============================================================
# 12. 原子性：verify 失败不留半成品（学习 Marvis 事务内自检，v2.4.2）
#     verify 前置到 register 之前；失败删文件，无 db/lineage 痕迹
# ============================================================
import tempfile as _tf
import fork_core.engine as _eng

_atom_tmp = _tf.mkdtemp(prefix="wb-atom-")
import fork_core.adapter_workbuddy as wb_mod2
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
with open(os.path.join(wb_mod2.PROJECTS_DIR, "Users-x-test", _src+".jsonl"), "w", encoding="utf-8") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_src,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_src,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_src,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")

_orig_vb = _eng.verify_branch
_eng.verify_branch = lambda *a, **k: ["forced failure"]
from fork_core.engine import create_fork as _cf
from fork_core.adapter_workbuddy import WorkBuddyAdapter as _WBA
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
_lin = json.load(open(wb_mod2.LINEAGE_PATH, encoding="utf-8")) if os.path.exists(wb_mod2.LINEAGE_PATH) else {"forks": []}
assert _n == 1 and len(_files) == 1 and not _lin.get("forks"), "verify 失败应不留痕迹"
shutil.rmtree(_atom_tmp)
print("✓ 原子性: verify 失败不留半成品（db/文件/lineage 全干净）")
# ============================================================
# 14. register 失败回滚方向（撤 db 副作用，保留合格文件）v2.4.3
# ============================================================
import fork_core.adapter_workbuddy as _wb4
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
with open(os.path.join(_wb4.PROJECTS_DIR, "Users-x-test", _rsrc+".jsonl"), "w", encoding="utf-8") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_rsrc,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_rsrc,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_rsrc,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")
from fork_core.adapter_workbuddy import WorkBuddyAdapter as _WBA3
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
import fork_core.adapter_workbuddy as _wb5
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
with open(os.path.join(_wb5.PROJECTS_DIR, "Users-x-test", _lsrc+".jsonl"), "w", encoding="utf-8") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_lsrc,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_lsrc,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_lsrc,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")
from fork_core.adapter_workbuddy import WorkBuddyAdapter as _WBA4
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
import fork_core.adapter_workbuddy as _wb3
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
with open(os.path.join(_wb3.PROJECTS_DIR, "Users-x-test", _dsrc+".jsonl"), "w", encoding="utf-8") as f:
    for l in [
        {"type":"message","role":"user","sessionId":_dsrc,"content":[{"type":"input_text","text":"hi"}]},
        {"type":"message","role":"assistant","sessionId":_dsrc,"content":[{"type":"output_text","text":"ok"}]},
        {"type":"message","role":"user","sessionId":_dsrc,"content":[{"type":"input_text","text":"打分支"}]},
    ]: f.write(json.dumps(l)+"\n")
from fork_core.adapter_workbuddy import WorkBuddyAdapter as _WBA2
_dr = _cf(_WBA2(), _dsrc, name="T", dry_run=True)  # 不应 UnboundLocalError
assert _dr.ok and _dr.verified, "dry-run 应 ok 且 verified"
assert _dr.cut == 2, "dry-run 应报截断点"
print(f"✓ dry_run 正常（截断 L{_dr.cut}，不崩溃）")
shutil.rmtree(_dr_tmp)
print("\n✅ WorkBuddy adapter 全部测试通过（含 dry_run/原子性/回滚/真库体检）")

# ============================================================
# 16. 默认截断点两种语义（v2.4.9）——判据 = "我是否就在被 fork 的那个会话里"
#
# 真实事故（2026-09-15，两例）：agent 在自己的会话里执行 `--session current` 打分支，
# 切点落在**本轮自己的叙述**上，分支多吞整段本轮内容（含"打分支"这条指令本身）：
#   · 3ccebc53（源 ec48e1ae）：实切 L18782，应为 L18763 —— 多吞 19 行；
#   · 5df71252（源 d6af6ce6）：实切 L8452，应为 L8424 —— 多吞 28 行。
# 且切点漂移：同一轮内 dry-run 报 18778、实跑写 18782（执行者据此"以为"截对了）。
# 根因 = v2.1.0 提交 82df3a5 删掉了"锚在最后一条 user 消息之前"的 4 行，只留全文件倒扫
# （那次改动是为了支持"分支再 fork 时整份复制"—— 两种语义都合法，故拆成两个函数分派）。
# ============================================================
import fork_core.adapter_workbuddy as _wb6
from fork_core.engine import (
    locate_last_reply as _llr,
    locate_before_current_turn as _lbct,
    create_fork as _cf6,
)

_sem_tmp = _tf.mkdtemp(prefix="wb-sem-")
_wb6.DB_PATH = os.path.join(_sem_tmp, "workbuddy.db")
_wb6.PROJECTS_DIR = os.path.join(_sem_tmp, "projects")
_wb6.LINEAGE_PATH = os.path.join(_sem_tmp, "fork.lineage.json")
os.makedirs(os.path.join(_wb6.PROJECTS_DIR, "Users-x-test"))
_sem_src = "SEM-0000-0000-0000-000000000001"
_sem_newer = "SEM-0000-0000-0000-000000000002"   # created_at 更大 → SQL 会选它


def _mk_session(sid, created_at):
    c = sqlite3.connect(_wb6.DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (id TEXT, cwd TEXT NOT NULL,
        user_id TEXT NOT NULL, title TEXT, custom_title TEXT,
        status TEXT NOT NULL DEFAULT 'Pending', created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, deleted_at INTEGER, is_playground INTEGER NOT NULL DEFAULT 0,
        last_activity_at INTEGER)""")
    c.execute("INSERT INTO sessions (id,cwd,user_id,status,created_at,updated_at,is_playground)"
              " VALUES (?,?,?,?,?,?,?)", (sid, "/tmp", "u", "working", created_at, created_at, 0))
    c.commit(); c.close()


def _write_session(sid, rows):
    with open(os.path.join(_wb6.PROJECTS_DIR, "Users-x-test", sid + ".jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


_mk_session(_sem_src, 1000)
_mk_session(_sem_newer, 2000)   # 更"新"的 working 会话：考 resolve_session 会不会认错

# 事故形状：上一轮完整收尾(L2) + 本轮指令(L3) + 本轮叙述(L7)
_mk_user = lambda t: {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}
_mk_asst = lambda t: {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": t}]}
_write_session(_sem_src, [
    _mk_user("第一轮的问题"),                                        # L1
    _mk_asst("第一轮的完整回答" + "x" * 200),                        # L2 ← 正确切点
    _mk_user("打分支"),                                             # L3 ← 本轮指令
    {"type": "reasoning", "text": "本轮思考"},                       # L4
    {"type": "function_call", "name": "bash", "arguments": "{}"},   # L5
    {"type": "function_call_result", "output": {"text": "ok"}},     # L6
    _mk_asst("正在打分支：先看引擎版本…"),                            # L7 ← 旧实现切在这
])

_a6 = _wb6.WorkBuddyAdapter()
_lines6 = _a6.read_lines(os.path.join(_wb6.PROJECTS_DIR, "Users-x-test", _sem_src + ".jsonl"))

# 16a. 两个定位函数的语义差异（这正是 bug 的机理）
assert _llr(_a6, _lines6) == (7, 7), "整份语义应切在末条 assistant 文本（L7）"
_cut6, _tot6, _note6 = _lbct(_a6, _lines6)
assert (_cut6, _tot6) == (2, 7), f"in-session 语义应切在上一轮结束（L2），实为 L{_cut6}"
assert "L3" in _note6 and "打分支" in _note6, f"note 应指明锚点行与原文：{_note6}"
print(f"✓ 定位语义: 整份→L7（吞本轮） / in-session→L2（正确） · {_note6}")

# 16b. 默认模式分派：无会话上下文 → 整份（旧行为，不回归）
assert _a6.running_session_id() != _sem_src, "测试环境不应自称在被 fork 的会话里"
_r_whole = _cf6(_a6, _sem_src, name="T", dry_run=True,
                backups_dir=os.path.join(_sem_tmp, "bk"))
assert _r_whole.cut == 7, f"无会话上下文应保持整份语义（L7），实为 L{_r_whole.cut}"
assert "whole" in _r_whole.how, _r_whole.how
print(f"✓ 默认模式(无会话上下文): cut=L{_r_whole.cut} · {_r_whole.how}")

# 16c. 默认模式分派：**我就在该会话里** → 上一轮输出结束（修复点）
os.environ["CLAUDE_SESSION_ID"] = _sem_src
try:
    assert _a6.running_session_id() == _sem_src
    _r_in = _cf6(_a6, _sem_src, name="T", dry_run=True,
                 backups_dir=os.path.join(_sem_tmp, "bk"))
    assert _r_in.cut == 2, f"会话内打分支应切在上一轮结束（L2），实为 L{_r_in.cut}"
    assert "in-session" in _r_in.how and "打分支" in _r_in.how, _r_in.how
    print(f"✓ 默认模式(会话内): cut=L{_r_in.cut} · {_r_in.how}")

    # 16d. resolve_session('current') 必须认"执行环境的会话"，而不是"最新的 working 会话"
    assert _a6.resolve_session("current") == _sem_src, \
        "current 应解析为执行环境所在会话（最新 working 是 …0002，会指错）"
    print("✓ resolve_session(current): 认执行环境会话，不误取最新的 working 会话")

    # 16e. 标识过期（环境里的会话已无 transcript）→ **硬失败**，不得回退到别的对话
    #      ⚠️ 2026-09-15 事故订正：本条原先断言 `resolve_session('current') == _sem_newer`
    #      （"过期标识应回退到产品存储"），并把"不抛 not found"当成兜底特性发绿勾。
    #      但"最新的 working 会话"**不等于**"我正在其中的这个对话"：实测同一命令 8 分钟内
    #      解析出两个不同源会话（19:35 命中本对话、19:43 打到「检索与审核」），产物是一个
    #      别的对话的分支。回退 = 静默打错对话，所以此期望是**缺陷本身**，改为硬失败。
    os.environ["CLAUDE_SESSION_ID"] = "NOT-EXIST-0000-0000-000000000000"
    assert _a6.running_session_id() == "NOT-EXIST-0000-0000-000000000000"
    try:
        _a6.resolve_session("current")
        raise AssertionError("过期标识必须硬失败，不得静默回退到别的对话")
    except SystemExit as _e:
        assert "找不到对应的对话文件" in str(_e), str(_e)
    print("✓ resolve_session(current): 过期标识硬失败（不再静默回退到别的对话）")

    # 16e2. 标识**完全缺失**（人从终端 / 环境没注入）→ 同样硬失败：没有标识就无法判定
    #       "我在哪个对话里"，猜错就打出另一个对话的分支。
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CODEBUDDY_SESSION_ID", None)
    os.environ.pop("BAGGAGE", None)
    assert _a6.running_session_id() is None
    try:
        _a6.resolve_session("current")
        raise AssertionError("无会话标识时必须硬失败，不得猜")
    except SystemExit as _e:
        assert "无法确定" in str(_e), str(_e)
    print("✓ resolve_session(current): 无会话标识硬失败（不猜）")

    # 16e3. 旧语义有显式入口，且不再冒充 current
    os.environ["CLAUDE_SESSION_ID"] = _sem_src
    assert _a6.resolve_session("latest-working") == _sem_newer, \
        "latest-working 才是「最新 working 会话」的显式入口"
    print("✓ resolve_session(latest-working): 旧语义已挪到显式入口")
finally:
    os.environ.pop("CLAUDE_SESSION_ID", None)

# 16f. 退化情形：无 user 消息 / 单轮会话 → 明确退回整份语义（不静默、不报错）
_sem2 = "SEM-0000-0000-0000-000000000003"
_write_session(_sem2, [{"type": "reasoning"}, _mk_asst("只有一条回复")])
_l2 = _a6.read_lines(os.path.join(_wb6.PROJECTS_DIR, "Users-x-test", _sem2 + ".jsonl"))
_c2, _t2, _n2 = _lbct(_a6, _l2)
assert (_c2, _t2) == (2, 2) and "无 user 消息" in _n2, (_c2, _n2)

_sem3 = "SEM-0000-0000-0000-000000000004"
_write_session(_sem3, [_mk_user("刚问完"), _mk_asst("刚答完")])
_l3 = _a6.read_lines(os.path.join(_wb6.PROJECTS_DIR, "Users-x-test", _sem3 + ".jsonl"))
_c3, _t3, _n3 = _lbct(_a6, _l3)
assert (_c3, _t3) == (2, 2) and "单轮会话" in _n3, (_c3, _n3)

# ⚠️ 单轮会话在"会话内"必须是**整份**：用户刚发话、助手刚答完，这一刻要打分支
#    （没有"本轮要排除"的语义落点），退回整份 = 保留全部内容，是正确的兜底。
#    【条款 24 已审 · 2026-09-15】兜底值 == 正确答案：单轮会话若按 in-session 排除最后一轮
#    ⇒ 产物是空分支（无用），故整份是唯一有语义的解；且引擎随件给 note，不静默。
os.environ["CLAUDE_SESSION_ID"] = _sem3
try:
    _r3 = _cf6(_a6, _sem3, name="T", dry_run=True, backups_dir=os.path.join(_sem_tmp, "bk"))
    assert _r3.cut == 2, _r3.cut
finally:
    os.environ.pop("CLAUDE_SESSION_ID", None)
print("✓ 退化兜底: 无 user 消息 / 单轮会话 → 明确退回整份（有 note，不静默）")
shutil.rmtree(_sem_tmp)
print("\n✅ 默认截断点语义测试通过（in-session vs whole 分派 + resolve_session 归属 + 退化兜底）")

"""pi / OpenClaw 谱系适配器契约测试（fixture 级，不依赖真实 pi 安装）。

固化的契约来自一手证据：
  - pi 官方 docs/session-format.md
  - @mariozechner/pi-coding-agent@0.73.1 dist/core/session-manager.js
自定位：技能根目录按 __file__ 推导，任何机器可直接跑。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fork_core.adapter_pi as pi_mod
from fork_core.adapter_pi import PiAdapter

# --- 造模拟 pi 会话（真实布局：<agent>/sessions/--<cwd 转义>--/<时间戳>_<id>.jsonl）---
tmpdir = tempfile.mkdtemp(prefix="pi-test-")
slug = "--Users-test-project--"
sess_dir = os.path.join(tmpdir, "sessions", slug)
os.makedirs(sess_dir, exist_ok=True)

src_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
UPSTREAM = "/some/upstream/99999999-9999-9999-9999-999999999999.jsonl"
# ⚠️ 文件名 ≠ 会话 id：带 ISO 时间戳前缀（: 和 . 已替换为 -）
file_name = "2026-09-14T03-05-40-014Z_" + src_id + ".jsonl"
path = os.path.join(sess_dir, file_name)

lines = [
    # 首行 header：官方校验只要求 type=="session" 且 id 为 string
    {"type": "session", "version": 3, "id": src_id, "timestamp": "2026-09-14T03:05:40.014Z",
     "cwd": "/Users/test/project", "parentSession": UPSTREAM},
    {"type": "message", "id": "a1b2c3d4", "parentId": None, "timestamp": "2026-09-14T03:05:41.000Z",
     "message": {"role": "user", "content": f"帮我看看会话 {src_id} 的问题"}},
    {"type": "message", "id": "b2c3d4e5", "parentId": "a1b2c3d4", "timestamp": "2026-09-14T03:05:42.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "内部推理（不应进 get_text）"},
         {"type": "text", "text": "好的，先查一下"},
         {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": f"echo {src_id}"}},
     ]}},
    {"type": "message", "id": "c3d4e5f6", "parentId": "b2c3d4e5", "timestamp": "2026-09-14T03:05:43.000Z",
     "message": {"role": "toolResult", "toolCallId": "call_1", "toolName": "bash",
                 "content": [{"type": "text", "text": f"输出含 {src_id}"}], "isError": False}},
    {"type": "message", "id": "d4e5f6a7", "parentId": "c3d4e5f6", "timestamp": "2026-09-14T03:05:44.000Z",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "方案二成本更低"}]}},
    {"type": "custom", "id": "e5f6a7b8", "parentId": "d4e5f6a7", "timestamp": "2026-09-14T03:05:45.000Z",
     "customType": "my-ext", "data": {"count": 1}},
]
with open(path, "w", encoding="utf-8") as f:
    for l in lines:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")

# 另一个"分支"产物：文件名是裸 uuid（引擎产出形态），header.parentSession 指向源
br_id = "11112222-3333-4444-5555-666677778888"
br_path = os.path.join(sess_dir, br_id + ".jsonl")
with open(br_path, "w", encoding="utf-8") as f:
    for l in [
        {"type": "session", "version": 3, "id": br_id, "timestamp": "2026-09-14T04:00:00.000Z",
         "cwd": "/Users/test/project", "parentSession": path},
        lines[1],
        lines[2],
    ]:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")

# 负向样本：首行不是合法 header（应被忽略）
bad_path = os.path.join(sess_dir, "2026-09-14T03-00-00-000Z_bad.jsonl")
with open(bad_path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "message", "id": "x", "message": {"role": "user", "content": "x"}}) + "\n")

# 注入临时 agent 目录（模块级 + 实例快照两处一致）
pi_mod.AGENT_DIR = tmpdir
pi_mod.SESSIONS_DIR_NAME = "sessions"
adapter = PiAdapter()

# 1. find_transcript：必须按 header.id 找（文件名带时间戳前缀）
p, s = adapter.find_transcript(src_id)
assert p == path and s == slug, f"find failed: {p}, {s}"
print("✓ find_transcript（文件名≠id，按 header.id 定位）")

# 2. 裸 uuid 文件名的产物同样能找到（引擎产出形态）
p2, _ = adapter.find_transcript(br_id)
assert p2 == br_path, f"find(bare uuid) failed: {p2}"
print("✓ find_transcript（裸 uuid 文件名同样可定位）")

# 3. resolve_session(current) → 最近修改
cur = adapter.resolve_session("current")
assert cur in (src_id, br_id), f"current resolved to {cur}"
print("✓ resolve_session(current)")

# 4. 消息判定
assert adapter.is_user_message(lines[1]) and not adapter.is_user_message(lines[2])
assert adapter.is_assistant_message(lines[2]) and not adapter.is_assistant_message(lines[1])
assert not adapter.is_user_message(lines[0])          # header 不是消息
assert not adapter.is_assistant_message(lines[5])     # custom 条目不是消息
print("✓ is_user_message / is_assistant_message")

# 5. get_text：content 为 str、为块数组、跳过 thinking/toolCall
assert adapter.get_text(lines[1]) == f"帮我看看会话 {src_id} 的问题"
t = adapter.get_text(lines[2])
assert t == "好的，先查一下" and "内部推理" not in t and "echo" not in t, f"get_text={t!r}"
assert adapter.get_text(lines[3]) == f"输出含 {src_id}"  # toolResult 也算可读文本
assert adapter.get_text(lines[0]) == ""                 # header 无文本
print("✓ get_text（str / 块数组 / 跳过 thinking 与 toolCall）")

# 6. get_request_id = 条目 id
assert adapter.get_request_id(lines[1]) == "a1b2c3d4"
assert adapter.get_request_id(lines[0]) is None
print("✓ get_request_id（= 条目 id）")

# 7. rewrite_ids：只改 header.id；**条目 id 与 parentId 一律不动**；parentSession 不动
old, new = src_id, "NEWID-0000-1111-2222-333333333333"
out, n = adapter.rewrite_ids([dict(o) for o in lines], old, new)
assert n == 1, f"应只替换 1 处（header.id），实际 {n}"
assert out[0]["id"] == new
assert out[0]["parentSession"] == UPSTREAM, "parentSession 是谱系指针，绝不能替换"
for i, o in enumerate(out[1:], 1):
    assert o.get("id") == lines[i].get("id"), f"L{i+1} 条目 id 被改动——会剪断 parentId 树"
    assert o.get("parentId") == lines[i].get("parentId"), f"L{i+1} parentId 被改动"
# 条目文本里的源 id 属于对话内容，不做替换（与官方 createBranchedSession 一致）
assert old in adapter.get_text(out[1]), "对话正文不应被改写"
print("✓ rewrite_ids（仅 header.id；条目 id/parentId/parentSession 原样保留）")

# 8. header 缺失时退化：条目 id 仍受保护
no_header = [dict(o) for o in lines[1:]]
out2, _ = adapter.rewrite_ids(no_header, old, new)
assert [o["id"] for o in out2] == [o["id"] for o in lines[1:]], "无 header 时条目 id 仍不可动"
print("✓ rewrite_ids（无 header 的异常文件：条目 id 仍受保护）")

# 9. list_all_sessions 返回 header.id，而非文件名
pairs = dict((sid, p) for p, sid in adapter.list_all_sessions())
assert src_id in pairs and br_id in pairs, f"list_all_sessions 漏会话：{list(pairs)}"
assert not any("2026-09-14T" in sid for sid in pairs), "把文件名当成了 session id"
assert "bad" not in " ".join(pairs), "非法 header 文件应被忽略"
print("✓ list_all_sessions（返回 header.id，跳过非法 header）")

# 10. load_session_meta：原生 parentSession 反查出上游 id；上游不存在时留空
meta = adapter.load_session_meta(br_id)
assert meta is not None and meta.parent_id == src_id, f"应反查出上游 id，实际 {meta and meta.parent_id}"
meta_src = adapter.load_session_meta(src_id)
assert meta_src.cwd == "/Users/test/project"
assert not meta_src.parent_id, f"上游文件不存在时 parent_id 应留空，实际 {meta_src.parent_id!r}"
print("✓ load_session_meta（原生谱系 parentSession → 上游 id）")

# 11. verify_storage 分级：有合法会话 → L2
items = adapter.verify_storage()
lv = {it.name: it.level for it in items}
assert lv.get("pi transcript 目录") == "L2", f"应达 L2：{lv}"
print("✓ verify_storage（有真实会话 → L2）")

# 12. 引擎级：locate_last_reply 能在 pi 行上定位（形状无关）
from fork_core.engine import locate_last_reply
real = adapter.read_lines(path)
cut, total = locate_last_reply(adapter, real)
assert cut == 5, f"cut={cut}（末条 assistant 在 L5，故截到 5）"
print("✓ locate_last_reply（引擎截断定位可用）")

# 13. 产品化文案已覆盖（不再是 WorkBuddy 口径）
assert "pi" in adapter.activation_hint() and "WorkBuddy" not in adapter.activation_hint()
assert "pi" in adapter.produce_hint()
assert adapter.backups_dir().startswith(tmpdir)
print("✓ 产品化文案与备份目录")

# 14. 全链路 create_fork：正文含源 id 也必须成功
#     （复核抓出的缺陷回归：只测 rewrite_ids 隔离态会漏掉 verify_branch 误拒）
from fork_core.engine import create_fork

r = create_fork(
    adapter,
    session_ref=src_id,
    name="全链路回归",
    backups_dir=os.path.join(tmpdir, "backups"),
)
assert r.ok and r.verified, "全链路 fork 失败"
assert os.path.exists(r.dst_path), f"产物不存在：{r.dst_path}"
assert r.cut == 5, f"cut={r.cut}（末条 assistant 在 L5）"
out_lines = adapter.read_lines(r.dst_path)
assert len(out_lines) == 5, f"产物行数 {len(out_lines)} != 5"
assert out_lines[0]["type"] == "session" and out_lines[0]["id"] == r.new_id
assert out_lines[0]["version"] == 3, "header.version 应随源保留"
assert src_id in adapter.get_text(out_lines[1]), "正文里的源 id 属对话内容，应原样保留"
print("✓ 全链路 create_fork（正文含源 id 不误拒；header.id 已换）")

# 15. 产物满足 pi 官方判据（官方靠这两条发现会话）
h = adapter._read_header(r.dst_path)
assert h and h["type"] == "session" and isinstance(h["id"], str)
assert h.get("parentSession") == UPSTREAM, "源 header 的原生谱系指针应原样保留"
print("✓ 产物 header 满足 pi 官方判据（文件名不影响发现）")

# 16. 登记 / 回滚
assert r.new_id in {b.id for b in adapter.list_branches()}
adapter.unregister_branch(r.new_id)
assert r.new_id not in {b.id for b in adapter.list_branches()}
print("✓ register_branch / unregister_branch")

print("\n✅ test_pi_adapter 全部通过（16 组断言）")

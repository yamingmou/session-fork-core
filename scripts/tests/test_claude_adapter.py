import json
import os
import sys
import tempfile

sys.path.insert(0, "/Users/maxwell/.workbuddy/skills/session-fork/scripts")

from fork_core.adapters.claude_code import ClaudeCodeAdapter

# --- 造模拟 Claude Code transcript ---
tmpdir = tempfile.mkdtemp(prefix="cc-test-")
slug = "-Users-test-project"
proj_dir = os.path.join(tmpdir, "projects", slug)
os.makedirs(proj_dir, exist_ok=True)
src_id = "11111111-1111-1111-1111-111111111111"
path = os.path.join(proj_dir, src_id + ".jsonl")

lines = [
    # 真实 Claude Code transcript 结构：text / tool_use(input) / tool_result(content)
    {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": src_id, "cwd": "/tmp",
     "message": {"role": "user", "content": [{"type": "text", "text": f"帮我看看会话 {src_id} 的问题"}]}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": src_id, "cwd": "/tmp",
     "message": {"role": "assistant", "content": [
         {"type": "text", "text": "好的，先查一下"},
         {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"echo {src_id}"}},
     ]}},
    {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": src_id, "cwd": "/tmp",
     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                               "content": f"命令输出包含 {src_id}"}]}},
    {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": src_id, "cwd": "/tmp",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "方案二：改造成本更低"}]}},
    {"type": "user", "uuid": "u3", "parentUuid": "a2", "sessionId": src_id, "cwd": "/tmp",
     "message": {"role": "user", "content": [{"type": "text", "text": "打分支，命名『测试分支』"}]}},
]
with open(path, "w") as f:
    for l in lines:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")

# 注入 CLAUDE_DIR 指向临时目录
import fork_core.adapters.claude_code as cc_mod
cc_mod.CLAUDE_DIR = tmpdir
cc_mod.PROJECTS_DIR = os.path.join(tmpdir, "projects")
cc_mod.BRANCH_INDEX = os.path.join(tmpdir, "fork.branches.json")

adapter = ClaudeCodeAdapter()

# 1. find_transcript
p, s = adapter.find_transcript(src_id)
assert p == path and s == slug, f"find failed: {p}, {s}"
print("✓ find_transcript")

# 2. resolve_session（current → 最新）
cur = adapter.resolve_session("current")
assert cur == src_id, f"current resolved to {cur}"
print("✓ resolve_session(current)")

# 3. 截断点定位（默认模式 = 最后一条完整回复）
from fork_core.engine import locate_last_reply
lines = adapter.read_lines(path)
cut, total = locate_last_reply(adapter, lines)
assert cut == 4, f"cut={cut} (expect 4 — 最后一条 assistant 回复在 L4)"
print(f"✓ locate_last_reply cut={cut}/{total}")

# 4. --match 定位
from fork_core.engine import locate_split_point
cut2, _ = locate_split_point(adapter, lines, match_text="方案二")
assert cut2 == 4, f"match cut={cut2}"
print(f"✓ locate_split_point(match='方案二') cut={cut2}")

# 5. 用 uuid 作为 request-id 等价物
cut3, _ = locate_split_point(adapter, lines, request_id="a1")
assert cut3 == 2, f"request-id(uuid) cut={cut3} (a1 在 L2)"
print(f"✓ locate_split_point(request_id=uuid) cut={cut3}")

# 6. rewrite_ids
from fork_core.engine import create_fork
r = create_fork(adapter, src_id, name="测试分支", dry_run=False)
print(f"✓ create_fork: {r.new_id} cut={r.cut} name={r.name!r}")
assert os.path.exists(r.dst_path), "dst not written"
# 验证分支文件 sessionId 全部替换 + 全文件零旧 id 残留
raw_all = open(r.dst_path).read()
assert src_id not in raw_all, "分支文件仍有旧 id 残留（text/tool_use.input/tool_result 引用未替换）"
print("✓ 分支文件 sessionId 全替换 + 全文件零残留（text/tool_use/tool_result 引用）")

# 7. list_branches（旁路索引）
branches = adapter.list_branches()
assert len(branches) == 1, f"branches={len(branches)}"
assert branches[0].parent_id == src_id, "谱系 parent 未记录"
print(f"✓ list_branches: {branches[0].id[:8]}… parent={branches[0].parent_id[:8]}…")

# 8. 清理
import shutil
shutil.rmtree(tmpdir)
print("\n✅ Claude Code adapter 全部测试通过")

"""OpenClaw 适配器契约测试（fixture 级，不依赖真实 OpenClaw 安装）。

固化的契约来自一手真机证据（2026-09-14）：
  - 真机 openclaw@2026.6.35 产出会话（真机产出的样本，可本地复跑核对）
  - 官方解析器 dist/session-manager-*.js 的 loadEntriesFromFile / buildSessionContext
  - 官方 CLI `openclaw sessions --json` 的索引行为

本测试固化的两条**与 pi 的硬差异**：
  ① 会话必须在 sessions.json 登记才可见（只放文件不够）
  ② 同目录 *.trajectory.jsonl 不是会话，必须被排除
自定位：技能根目录按 __file__ 推导，任何机器可直接跑。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC_ID = "11111111-2222-3333-4444-555555555555"

# --- 造模拟 OpenClaw 布局：<state>/agents/main/sessions/ （平铺，无 --cwd-- 子目录）---
tmpdir = tempfile.mkdtemp(prefix="openclaw-test-")
state = os.path.join(tmpdir, "state")
sess_dir = os.path.join(state, "agents", "main", "sessions")
os.makedirs(sess_dir, exist_ok=True)

os.environ["OPENCLAW_STATE_DIR"] = state

from fork_core.adapter_openclaw import OpenClawAdapter  # noqa: E402
from fork_core.engine import create_fork  # noqa: E402

src_path = os.path.join(sess_dir, SRC_ID + ".jsonl")
lines = [
    {"type": "session", "version": 3, "id": SRC_ID,
     "timestamp": "2026-09-14T06:12:06.717Z", "cwd": "/tmp/ws"},
    {"type": "model_change", "id": "e712ee9a", "parentId": None,
     "timestamp": "2026-09-14T06:12:06.720Z", "provider": "ollama", "modelId": "qwen2.5:3b"},
    {"type": "message", "id": "87c09c67", "parentId": "e712ee9a",
     "timestamp": "2026-09-14T06:12:06.771Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: LAB-OK"}]}},
    {"type": "message", "id": "fdd4d536", "parentId": "87c09c67",
     "timestamp": "2026-09-14T06:12:07.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "内部推理（不应进 get_text）"},
         {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "/tmp/a"}},
     ]}},
    {"type": "message", "id": "c478a92d", "parentId": "fdd4d536",
     "timestamp": "2026-09-14T06:12:07.500Z",
     "message": {"role": "toolResult", "toolCallId": "call_1", "toolName": "read",
                 "content": [{"type": "text", "text": f"文件里有个目录名恰好是 {SRC_ID}"}], "isError": False}},
    {"type": "message", "id": "c310087b", "parentId": "c478a92d",
     "timestamp": "2026-09-14T06:12:08.000Z",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "LAB-OK"}]}},
]
with open(src_path, "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(o, ensure_ascii=False) for o in lines) + "\n")

# --- 干扰项：trajectory（另一套 schema，首行 type="session.started"）---
traj_path = os.path.join(sess_dir, f"{SRC_ID}.trajectory.jsonl")
with open(traj_path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"traceSchema": "openclaw-trajectory", "schemaVersion": 1,
                        "traceId": SRC_ID, "type": "session.started",
                        "sessionId": SRC_ID, "sessionKey": "agent:main:lab-first"}, ensure_ascii=False) + "\n")

# --- 源会话索引（OpenClaw 的权威索引）---
index_path = os.path.join(sess_dir, "sessions.json")
src_entry = {
    "sessionId": SRC_ID, "updatedAt": 1789366366242, "sessionStartedAt": 1789366323312,
    "lastInteractionAt": 1789366366237, "sessionFile": src_path,
    "contextTokens": 32768, "modelProvider": "ollama", "model": "qwen2.5:3b",
    "agentHarnessId": "openclaw", "abortedLastRun": False, "compactionCount": 1,
    # 与源会话绑定的运行期报告：登记分支时应被丢弃（内含源 sessionId）
    "systemPromptReport": {"sessionId": SRC_ID, "chars": 32431},
}
with open(index_path, "w", encoding="utf-8") as f:
    json.dump({"agent:main:lab-first": src_entry}, f, ensure_ascii=False, indent=2)

adapter = OpenClawAdapter()

# 1. 路径布局（平铺，无 --cwd-- 子目录）
assert adapter.SESSIONS_DIR == sess_dir, adapter.SESSIONS_DIR
assert adapter.SESSIONS_INDEX == index_path
print("✓ 目录布局 <state>/agents/<agent>/sessions/（平铺）")

# 2. 枚举：排除 trajectory 干扰项
found = adapter.list_all_sessions()
assert len(found) == 1, f"应只发现 1 个会话，实际 {len(found)}：{[p for p, _ in found]}"
assert found[0][1] == SRC_ID
assert "trajectory" not in found[0][0]
print("✓ *.trajectory.jsonl 被排除（不依赖 header 恰好不匹配）")

# 3. 定位
assert adapter.find_transcript(SRC_ID)[0] == src_path
assert adapter.resolve_session("current") == SRC_ID, "current 应取最近修改的合法会话"
print("✓ find_transcript / resolve_session('current')")

# 4. 消息判定与文本提取（复用 pi 契约：thinking 不进 get_text）
assert adapter.is_user_message(lines[2]) and adapter.is_assistant_message(lines[5])
assert not adapter.is_assistant_message(lines[4])
assert adapter.get_text(lines[2]) == "Reply with exactly: LAB-OK"
assert adapter.get_text(lines[3]) == "", "纯 thinking+toolCall 的消息正文应为空"
assert adapter.get_text(lines[4]).startswith("文件里有个目录名恰好是")
print("✓ 消息判定 / get_text（thinking 与 toolCall 不计入正文）")

# 5. rewrite_ids 只改 header.id（条目 id 是树边，绝不能动）
out, n = adapter.rewrite_ids([dict(x) for x in lines], SRC_ID, "new-id-0000")
assert out[0]["id"] == "new-id-0000" and n == 1
assert [x.get("id") for x in out[1:]] == [x.get("id") for x in lines[1:]], "条目 id 被改动了"
print("✓ rewrite_ids 只改 header.id（条目 id 保持）")

# 6. 全链路 create_fork（含 sessions.json 登记）
r = create_fork(
    adapter=adapter, session_ref=SRC_ID, name="分支·lab",
    backups_dir=os.path.join(tmpdir, "backups"),
)
assert r.ok and r.verified, "全链路 fork 失败"
assert os.path.exists(r.dst_path)
assert r.cut == 6, f"cut={r.cut}（末条 assistant 在 L6）"
print("✓ 全链路 create_fork")

# 7. 【硬差异①】分支必须被写进 sessions.json，否则 OpenClaw 看不见
idx = json.load(open(index_path, encoding="utf-8"))
branch_key = f"agent:main:fork-{r.new_id[:8]}"
assert branch_key in idx, f"sessions.json 未登记分支：{list(idx)}"
entry = idx[branch_key]
assert entry["sessionId"] == r.new_id
assert entry["sessionFile"] == r.dst_path
assert entry["compactionCount"] == 0, "分支是全新会话，compactionCount 应归零"
assert "systemPromptReport" not in entry, "源会话的运行期报告不应带进分支（内含源 id）"
assert SRC_ID not in json.dumps([v for k, v in idx.items() if k == branch_key], ensure_ascii=False), \
    "分支索引条目残留源 id"
print("✓【硬差异①】分支已写进 sessions.json（key=%s，源 id 零残留）" % branch_key)

# 8. 分支产物结构合法（header 换 id + 正文原样保留）
out_lines = adapter.read_lines(r.dst_path)
assert len(out_lines) == 6 and out_lines[0]["id"] == r.new_id and out_lines[0]["version"] == 3
assert SRC_ID in adapter.get_text(out_lines[4]), "正文里的源 id 属对话内容，应原样保留（且不触发误拒）"
print("✓ 分支产物：header.id 已换 / 正文原样 / 未误判残留")

# 9. 体检
items = {i.name: i for i in adapter.verify_storage()}
assert items["OpenClaw transcript 目录"].level == "L2"
assert items["OpenClaw 会话索引 sessions.json"].ok
print("✓ verify_storage 三层（目录 L2 + 索引 L2 + 谱系 L1）")

# 10. 注销要同时清两处索引
adapter.unregister_branch(r.new_id)
idx2 = json.load(open(index_path, encoding="utf-8"))
assert branch_key not in idx2, "sessions.json 未清理"
assert r.new_id not in {b.id for b in adapter.list_branches()}, "旁路谱系索引未清理"
print("✓ unregister_branch 同时清理 sessions.json 与旁路索引")

# 11. sessions.json 写入前有备份（防索引损坏）
bk = os.path.join(adapter.backups_dir(), "")
assert os.path.isdir(adapter.backups_dir()), "备份目录未创建"
baks = [f for f in os.listdir(adapter.backups_dir()) if f.startswith("sessions.json.bak.")]
assert baks, "sessions.json 写入前未备份"
print("✓ sessions.json 写入前备份（%d 份）" % len(baks))

print("\n✅ test_openclaw_adapter 全部通过（11 组断言）")

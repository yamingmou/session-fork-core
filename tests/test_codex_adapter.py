"""Codex 适配器契约测试（fixture 级，不依赖本机 Codex 安装）。

固化的一手证据来自 2026-09-14 真机取证 + `codex exec fork` 原生分叉对照
（取证记录、实验脚本、快照与 mock 录制均为一手可复跑材料）。

覆盖的**每条断言都对应一个实测事实**，注释里写明出处，防止后人凭想象改断言。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ CODEX_HOME 必须在构造 adapter 前设好（__init__ 读环境变量）
tmpdir = tempfile.mkdtemp(prefix="codex-test-")
os.environ["CODEX_HOME"] = tmpdir

from fork_core.adapter_codex import CodexAdapter  # noqa: E402

SESS_DIR = os.path.join(tmpdir, "sessions", "2026", "09", "14")
ARCH_DIR = os.path.join(tmpdir, "archived_sessions")
os.makedirs(SESS_DIR, exist_ok=True)
os.makedirs(ARCH_DIR, exist_ok=True)

#: 分叉产物应落的目录 = **分叉当天**（本地时区），不是源文件所在的日期目录。
#: 原生分叉实测行为：产物落在 `sessions/<today 本地 YYYY/MM/DD>/`（真机取证，
#: E1/E2 两次实测，含"meta 是 Z 时间而文件名是本地时间"的对照）。
#: ⚠️ 这里**必须按当前日期算**，不能写死：写死会让断言只在"今天恰好等于那个日期"时成立
#: —— 2026-09-15 跨零点实测，昨天全绿、今天直接红（源日期 09/14 ≠ 分叉日 09/15）。
FORK_DAY_DIR = os.path.join(tmpdir, "sessions", *time.strftime("%Y/%m/%d").split("/"))

SRC_ID = "01a09ec0-0b41-7d63-9241-907f464077a6"
OTHER_ID = "01a0535f-d435-7e73-8f38-660b3ea3ebd0"
WIN_TAIL = "8950dc628136"
SRC_WINDOW = SRC_ID[:23] + "-" + WIN_TAIL     # 实测形态：前 4 组 + 12 位 hex


def _meta(sid, extra=None):
    p = {
        "session_id": sid, "id": sid,
        "timestamp": "2026-09-14T07:09:41.087Z",
        "cwd": f"/tmp/work/{sid}", "originator": "codex_exec",
        "cli_version": "0.150.0-alpha.8", "source": "exec", "thread_source": "user",
        "model_provider": "ollama", "history_mode": "paginated",
        "base_instructions": {"text": "You are a coding agent running in the Codex CLI"},
        "context_window": {"window_id": SRC_WINDOW},
    }
    if extra:
        p.update(extra)
    return {"type": "session_meta", "ordinal": 0,
            "timestamp": "2026-09-14T07:09:41.087Z", "payload": p}


def _msg(sid, role, text, mid):
    o = {"type": "response_item", "timestamp": "2026-09-14T07:09:42.000Z",
         "payload": {"type": "message", "role": role,
                     "id": mid, "thread_id": sid,
                     "content": [{"type": "output_text" if role == "assistant" else "input_text",
                                  "text": text}]}}
    if role == "assistant":
        o["payload"]["content"][0]["type"] = "output_text"
    return o


# --- 源会话：session_meta + developer + 2 处注入型 role=user + 真 user/assistant ---
DEV_TEXT = ("<skills_instructions>\n## Skills\n</skills_instructions>\n"
            "<permissions instructions>\nwritable: /tmp/visualizations/2026/09/14/" + SRC_ID)
src_lines = [
    _meta(SRC_ID),
    # 注入项 1：environment_context（role=user，但**不是**用户输入 —— 发现 1）
    _msg(SRC_ID, "user", "<environment_context>\n  <cwd>/tmp</cwd>\n</environment_context>", "msg_env_1"),
    # 开发者提示词里**逐字嵌着**沙箱目录路径（含会话 id —— 发现 3）
    {"type": "response_item", "timestamp": "2026-09-14T07:09:42.5Z",
     "payload": {"type": "message", "role": "developer", "id": "msg_dev_1",
                 "content": [{"type": "input_text", "text": DEV_TEXT}]}},
    # 真用户消息
    _msg(SRC_ID, "user", "只回复两个字：收到", "msg_u_1"),
    # 事件行里带 thread_id（59 处里的一处 —— 发现 3）
    {"type": "event_msg", "timestamp": "2026-09-14T07:09:43.0Z",
     "payload": {"type": "item_completed", "thread_id": SRC_ID,
                 "item": {"type": "UserMessage", "id": "01a09ec0-4d37-7140-9092-6ee65687d845",
                          "content": [{"type": "text", "text": "只回复两个字：收到"}]}}},
    # ⭐ 镜像重复：同一文本又被写成 event_msg/user_message（发现 2）
    {"type": "event_msg", "timestamp": "2026-09-14T07:09:43.1Z",
     "payload": {"type": "user_message", "thread_id": SRC_ID, "message": "只回复两个字：收到"}},
    _msg(SRC_ID, "assistant", "收到", "msg_a_1"),
    {"type": "event_msg", "timestamp": "2026-09-14T07:09:44.0Z",
     "payload": {"type": "token_count", "thread_id": SRC_ID}},
]

src_path = os.path.join(SESS_DIR, f"rollout-2026-09-14T15-09-41-{SRC_ID}.jsonl")
with open(src_path, "w", encoding="utf-8") as f:
    for l in src_lines:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")

# --- 另一个源会话（用于 current / 排序判定），放归档目录（平铺布局）---
other_lines = [
    _meta(OTHER_ID),
    _msg(OTHER_ID, "user", "你会压缩视频吗", "msg_u_2"),
    _msg(OTHER_ID, "assistant", "会。", "msg_a_2"),
]
other_path = os.path.join(ARCH_DIR, f"rollout-2026-08-30T23-53-04-{OTHER_ID}.jsonl")
with open(other_path, "w", encoding="utf-8") as f:
    for l in other_lines:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")

# --- 最小 state_5.sqlite：threads 用**真实 38 列**（防止测试比现实更宽松）---
THREAD_COLS = [
    ("id", "TEXT PRIMARY KEY"), ("rollout_path", "TEXT NOT NULL"),
    ("created_at", "INTEGER NOT NULL"), ("updated_at", "INTEGER NOT NULL"),
    ("source", "TEXT NOT NULL"), ("model_provider", "TEXT NOT NULL"),
    ("cwd", "TEXT NOT NULL"), ("title", "TEXT NOT NULL"),
    ("sandbox_policy", "TEXT NOT NULL"), ("approval_mode", "TEXT NOT NULL"),
    ("tokens_used", "INTEGER NOT NULL DEFAULT 0"),
    ("has_user_event", "INTEGER NOT NULL DEFAULT 0"),
    ("archived", "INTEGER NOT NULL DEFAULT 0"), ("archived_at", "INTEGER"),
    ("git_sha", "TEXT"), ("git_branch", "TEXT"), ("git_origin_url", "TEXT"),
    ("cli_version", "TEXT NOT NULL DEFAULT ''"),
    ("first_user_message", "TEXT NOT NULL DEFAULT ''"),
    ("agent_nickname", "TEXT"), ("agent_role", "TEXT"),
    ("memory_mode", "TEXT NOT NULL DEFAULT 'enabled'"),
    ("model", "TEXT"), ("reasoning_effort", "TEXT"), ("agent_path", "TEXT"),
    ("created_at_ms", "INTEGER"), ("updated_at_ms", "INTEGER"),
    ("thread_source", "TEXT"), ("preview", "TEXT NOT NULL DEFAULT ''"),
    ("recency_at", "INTEGER NOT NULL DEFAULT 0"),
    ("recency_at_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("history_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("name", "TEXT"), ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("thread_section_id", "TEXT"), ("section_position", "INTEGER"),
    ("section_entered_at_ms", "INTEGER"), ("project_id", "TEXT"),
]
con = sqlite3.connect(os.path.join(tmpdir, "state_5.sqlite"))
con.execute(f"CREATE TABLE threads ({', '.join(f'{c} {t}' for c, t in THREAD_COLS)})")
for sid, path, ts in ((SRC_ID, src_path, 1789369781), (OTHER_ID, other_path, 1788105191)):
    con.execute(
        "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, "
        "model_provider, cwd, title, sandbox_policy, approval_mode, cli_version, "
        "first_user_message, memory_mode, model, history_mode, preview, recency_at, "
        "created_at_ms, recency_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, path, ts, ts, "exec", "ollama", f"/tmp/work/{sid}", "测试标题",
         '{"type":"read-only"}', "never", "0.150.0-alpha.8", "只回复两个字：收到",
         "disabled", "qwen2.5:3b", "paginated", "只回复两个字：收到", ts, ts * 1000, ts * 1000))
con.commit()
con.close()

con = sqlite3.connect(os.path.join(tmpdir, "thread_history_1.sqlite"))
con.execute("""CREATE TABLE thread_history_projection_state (
    thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER NOT NULL,
    next_rollout_ordinal INTEGER NOT NULL)""")
con.commit()
con.close()

adapter = CodexAdapter()

# ----------------------------------------------------------------------
# 1. 定位：文件名尾缀 == 会话 id，不能用 fn[:-6]
# ----------------------------------------------------------------------
p, s = adapter.find_transcript(SRC_ID)
assert p == src_path, f"find_transcript 失败: {p}"
assert s == tmpdir, f"slug 应为 CODEX_HOME: {s}"
assert adapter.find_transcript("no-such-id") == (None, None)
print("✓ find_transcript（文件名尾缀==id；三层日期目录）")

# 归档目录（平铺）同样可定位
p_other, _ = adapter.find_transcript(OTHER_ID)
assert p_other == other_path, f"归档会话定位失败: {p_other}"
print("✓ find_transcript（archived_sessions 平铺布局）")

# list_all_sessions 的 id 必须从文件名尾部切，而不是去掉 .jsonl
pairs = {sid: path for path, sid in adapter.list_all_sessions()}
assert pairs.get(SRC_ID) == src_path and pairs.get(OTHER_ID) == other_path, pairs
assert not any(k.endswith(".jsonl") for k in pairs), "id 不能带 .jsonl 后缀"
assert all(len(k) == 36 for k in pairs), f"id 长度应 36：{list(pairs)}"
print("✓ list_all_sessions（按文件名尾缀取 id，两个目录都覆盖）")

# resolve_session(current) → recency_at 最大且未归档
assert adapter.resolve_session("current") == SRC_ID
assert adapter.resolve_session("literally-an-id") == "literally-an-id"
print("✓ resolve_session(current / 直传)")

# ----------------------------------------------------------------------
# 2. 消息判定：注入项必须被排除（发现 1）；镜像族不得计入（发现 2）
# ----------------------------------------------------------------------
assert adapter.is_user_message(src_lines[3]), "真用户消息应判定为 user"
assert not adapter.is_user_message(src_lines[1]), "<environment_context> 是注入，不能算用户消息"
assert not adapter.is_user_message(src_lines[5]), "event_msg/user_message 是镜像，不能计入"
assert adapter.is_assistant_message(src_lines[6]) and not adapter.is_assistant_message(src_lines[3])
assert not adapter.is_assistant_message(src_lines[2]), "developer 不是 assistant"
assert not adapter.is_user_message(src_lines[0]), "session_meta 不是消息"
print("✓ is_user_message / is_assistant_message（注入项排除 + 镜像族不计）")

# 只数一遍：源会话恰有 1 条 user + 1 条 assistant
n_user = sum(1 for o in src_lines if adapter.is_user_message(o))
n_asst = sum(1 for o in src_lines if adapter.is_assistant_message(o))
assert (n_user, n_asst) == (1, 1), f"轮次计数错误：user={n_user} asst={n_asst}（镜像重复会翻倍）"
print("✓ 轮次计数不翻倍（event_msg 镜像族被排除）")

# get_text
assert adapter.get_text(src_lines[3]) == "只回复两个字：收到"
assert adapter.get_text(src_lines[6]) == "收到"
assert adapter.get_text(src_lines[1]).startswith("<environment_context>")
assert adapter.get_text(src_lines[0]) == ""
print("✓ get_text（input_text / output_text 块）")

# get_request_id = payload.id，且仅 message 条目
assert adapter.get_request_id(src_lines[3]) == "msg_u_1"
assert adapter.get_request_id(src_lines[0]) is None
assert adapter.get_request_id(src_lines[4]) is None, "event_msg 没有条目锚点"
print("✓ get_request_id（payload.id，仅 message）")

# ----------------------------------------------------------------------
# 3. rewrite_ids：thread_id / session_id / content / window_id 全覆盖
# ----------------------------------------------------------------------
NEW_ID = "11111111-2222-4333-8444-555555555555"
rw, n = adapter.rewrite_ids(json.loads(json.dumps(src_lines)), SRC_ID, NEW_ID)
blob = json.dumps(rw, ensure_ascii=False)
assert SRC_ID not in blob, f"仍有源 id 残留：{blob.count(SRC_ID)} 处"
assert n >= 5, f"替换次数偏低：{n}"
# content 里的注入渲染文本必须跟着改（否则分支的权限提示会指向源的沙箱目录）
assert NEW_ID in json.dumps(rw[2], ensure_ascii=False), "developer content 内的路径未改写"
assert NEW_ID in json.dumps(rw[1], ensure_ascii=False), "user 注入渲染文本未改写"
# thread_id 必须改（59 处里的一处）
assert rw[4]["payload"]["thread_id"] == NEW_ID, "event_msg.thread_id 未改写"
print(f"✓ rewrite_ids（{n} 处替换，content/thread_id/session_id 全覆盖）")

# window_id：不包含完整会话 id（前 23 字符 + 12 hex）→ 通用替换命不中，必须专项重算
w_new = rw[0]["payload"]["context_window"]["window_id"]
assert w_new != SRC_WINDOW and w_new.startswith(NEW_ID[:23] + "-"), f"window_id 未重算: {w_new}"
assert len(w_new) == 36 and w_new.count("-") == 4, f"window_id 形态错误: {w_new!r}"
print("✓ rewrite_ids（context_window.window_id 专项重算，形态正确无畸形连字符）")

# 血缘键豁免：forked_from_id / history_base 允许保留源 id
lineage_lines = json.loads(json.dumps(src_lines))
lineage_lines[0]["payload"]["forked_from_id"] = SRC_ID
lineage_lines[0]["payload"]["history_base"] = {"thread_id": SRC_ID,
                                               "end_ordinal_exclusive": 8,
                                               "end_byte_offset": 1234}
rw2, _ = adapter.rewrite_ids(lineage_lines, SRC_ID, NEW_ID)
assert rw2[0]["payload"]["forked_from_id"] == SRC_ID, "血缘字段不该被改写"
assert rw2[0]["payload"]["history_base"]["thread_id"] == SRC_ID, "history_base 是谱系指针"
print("✓ rewrite_ids（_RAW_KEYS 血缘键豁免：forked_from_id / history_base）")

# ----------------------------------------------------------------------
# 4. write_branch：自包含分支的两处 header 归一化
# ----------------------------------------------------------------------
# 模拟"源本身是分支"：带 history_base 的源被 fork 时，产物必须丢弃它，
# 否则产品会把祖父历史再拼一遍 → 历史重复。
src_is_fork = json.loads(json.dumps(src_lines))
src_is_fork[0]["payload"]["history_base"] = {"thread_id": "aaaa", "end_ordinal_exclusive": 5,
                                             "end_byte_offset": 111}
src_is_fork[0]["payload"]["history_mode"] = "legacy"
src_is_fork[0]["payload"]["forked_from_id"] = "aaaa"
adapter.branch_target(src_path, NEW_ID)
tmp_out = os.path.join(SESS_DIR, "probe.tmp")
adapter.write_branch(tmp_out, src_is_fork)
out = [json.loads(l) for l in open(tmp_out, encoding="utf-8") if l.strip()]
assert "history_base" not in out[0]["payload"], "自包含产物必须丢弃源 history_base"
assert out[0]["payload"]["history_mode"] == "paginated", "history_mode 应归一为 paginated"
assert out[0]["payload"]["forked_from_id"] == SRC_ID, "应写入本分支自己的血缘（= 直接源）"
assert out[0]["payload"]["session_id"] == out[0]["payload"]["id"]
os.remove(tmp_out)
print("✓ write_branch（丢弃源 history_base / 归一 history_mode / 写 forked_from_id）")

# ----------------------------------------------------------------------
# 5. branch_target：布局与命名对齐原生分叉
# ----------------------------------------------------------------------
dst = adapter.branch_target(src_path, NEW_ID)
assert os.path.dirname(dst) == FORK_DAY_DIR, (
    f"日期目录不对: {dst}（应落在分叉当天 {FORK_DAY_DIR}，不是源文件所在目录）")
base = os.path.basename(dst)
assert base.startswith("rollout-") and base.endswith(NEW_ID + ".jsonl"), base
stamp = base[len("rollout-"):-len(NEW_ID) - len(".jsonl") - 1]
assert len(stamp) == 19 and stamp[10] == "T", f"时间戳形态应为 %Y-%m-%dT%H-%M-%S: {stamp!r}"
print(f"✓ branch_target（sessions/YYYY/MM/DD/rollout-<本地ISO>-<id>.jsonl：{base[:34]}…）")

# ----------------------------------------------------------------------
# 6. register_branch / unregister_branch：两层索引的往返
# ----------------------------------------------------------------------
meta = adapter.load_session_meta(SRC_ID)
assert meta and meta.id == SRC_ID and meta.cwd.endswith(SRC_ID)
# engine 的调用约定：dst 已落位，register 再补索引
adapter.write_branch(dst, rw[:6])
adapter.register_branch(meta, NEW_ID, dst, "分支·测试", parent_id=SRC_ID, at_seq=6)

row = dict(zip([c for c, _ in THREAD_COLS],
               sqlite3.connect(os.path.join(tmpdir, "state_5.sqlite"))
               .execute("SELECT * FROM threads WHERE id=?", (NEW_ID,)).fetchone()))
assert row["rollout_path"] == os.path.abspath(dst), row["rollout_path"]
assert row["title"] == "分支·测试" and row["preview"] == "分支·测试"
assert row["history_mode"] == "paginated", "分支自包含完整历史 → paginated"
assert row["archived"] == 0 and row["is_pinned"] == 0
assert row["source"] == "exec" and row["model_provider"] == "ollama", "应与源行一致"
# rollout_path 必须在 CODEX_HOME 之内（实测：在外则 codex 拒绝，code -32600）
assert row["rollout_path"].startswith(tmpdir + os.sep), "rollout_path 必须在 $CODEX_HOME 内"
print("✓ register_branch（threads 新行：显式列名、绝对路径、home 内）")

con = sqlite3.connect(os.path.join(tmpdir, "thread_history_1.sqlite"))
proj = con.execute("SELECT next_rollout_byte_offset, next_rollout_ordinal "
                   "FROM thread_history_projection_state WHERE thread_id=?",
                   (NEW_ID,)).fetchone()
con.close()
assert proj == (0, 0), f"投影游标应为 (0,0)=尚未消费，实为 {proj}"
print("✓ register_branch（投影游标 (0,0)：不伪造派生状态，交由产品 ingest）")

idx = adapter._read_index()
assert len(idx["branches"]) == 1 and idx["branches"][0]["at_seq"] == 6
assert idx["branches"][0]["artifact_lines"] == 6
print("✓ register_branch（旁路谱系：parent_id / at_seq / 产物尺寸）")

# 分支能被枚举、能被 --list 看到
assert NEW_ID in {sid for _p, sid in adapter.list_all_sessions()}, "新分支应可被枚举"
assert [b.id for b in adapter.list_branches()] == [NEW_ID]
assert adapter.is_branch_name("分支·测试")
print("✓ list_all_sessions / list_branches / is_branch_name")

adapter.unregister_branch(NEW_ID)
assert sqlite3.connect(os.path.join(tmpdir, "state_5.sqlite")).execute(
    "SELECT COUNT(*) FROM threads WHERE id=?", (NEW_ID,)).fetchone()[0] == 0
assert sqlite3.connect(os.path.join(tmpdir, "thread_history_1.sqlite")).execute(
    "SELECT COUNT(*) FROM thread_history_projection_state WHERE thread_id=?",
    (NEW_ID,)).fetchone()[0] == 0
assert adapter._read_index()["branches"] == []
print("✓ unregister_branch（threads + projection_state + 旁路谱系 三处全清）")

# ----------------------------------------------------------------------
# 7. 体检项：契约要点必须出现在 verify_storage 里
# ----------------------------------------------------------------------
items = adapter.verify_storage()
names = " ".join(i.name for i in items)
assert any("rollout" in i.detail for i in items)
assert any("code -32600" in i.detail for i in items), "rollout_path 必须在 home 内（实测）"
assert any("ordinal" in i.name for i in items), "ordinal 顶层键契约应在体检项中"
assert any("thread_spawn_edges" in i.name for i in items)
assert all(isinstance(i.ok, bool) and i.level for i in items)
print(f"✓ verify_storage（{len(items)} 项，契约要点齐全）")

# ----------------------------------------------------------------------
# 8. 截断语义：locate_last_reply 落在最后一条 assistant，而非尾部事件行
# ----------------------------------------------------------------------
from fork_core.engine import locate_last_reply  # noqa: E402

lines = adapter.read_lines(src_path)
cut, total = locate_last_reply(adapter, lines)
assert total == len(src_lines) == 8
assert cut == 7, f"应截到 assistant(第7行) 而非 token_count(第8行)，实为 {cut}"
assert adapter.is_assistant_message(lines[cut - 1])
print("✓ locate_last_reply（截到 assistant 回复末尾，尾部事件行不参与）")

# 引擎级校验：产物行数 == cut 且零残留
from fork_core.engine import verify_branch  # noqa: E402

rw3, _ = adapter.rewrite_ids(lines[:cut], SRC_ID, NEW_ID)
vpath = os.path.join(SESS_DIR, "verify.tmp")
adapter.write_branch(vpath, rw3)
errs = verify_branch(adapter, vpath, NEW_ID, cut, SRC_ID)
os.remove(vpath)
assert errs == [], f"verify_branch 应零错误，实为 {errs}"
print("✓ verify_branch（行数一致 / 零残留 / 末行 assistant 完整）")

shutil.rmtree(tmpdir, ignore_errors=True)
print("\n✅ Codex 适配器契约测试全部通过")

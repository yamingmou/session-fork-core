import json
import os
import sys
import tempfile

# 自定位：把技能根目录（本文件的上一级）加入模块搜索路径，任何机器都能直接跑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core.adapter_claude_code import ClaudeCodeAdapter

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
with open(path, "w", encoding="utf-8") as f:
    for l in lines:
        f.write(json.dumps(l, ensure_ascii=False) + "\n")

# 注入 CLAUDE_DIR 指向临时目录
import fork_core.adapter_claude_code as cc_mod
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
raw_all = open(r.dst_path, encoding="utf-8").read()
assert src_id not in raw_all, "分支文件仍有旧 id 残留（text/tool_use.input/tool_result 引用未替换）"
print("✓ 分支文件 sessionId 全替换 + 全文件零残留（text/tool_use/tool_result 引用）")

# 7. list_branches（旁路索引）
branches = adapter.list_branches()
assert len(branches) == 1, f"branches={len(branches)}"
assert branches[0].parent_id == src_id, "谱系 parent 未记录"
print(f"✓ list_branches: {branches[0].id[:8]}… parent={branches[0].parent_id[:8]}…")

# 9. is_human_turn：断点由"用户指向哪儿"决定，不由"指令行被存成什么形态"决定
#
# 背景（Claude Code 官方路径）：回合还在跑时提交的输入不写成 type:"user"，
# 而落成 type:"attachment" + attachment.type:"queued_command"。
# 旧实现只认 type=="user" ⇒ 锚点跳过它、落到再上一轮 ⇒ 切点前移一整轮、静默少一段上下文。
# 判据依据官方 u3() / L_() / dOs()（见 adapter_claude_code.is_human_turn 的 docstring）。
import shutil
from fork_core.engine import locate_before_current_turn

_ht_dir = tempfile.mkdtemp(prefix="cc-human-turn-")
_ht_path = os.path.join(_ht_dir, "t.jsonl")


def _a_text(t):
    return {"type": "assistant", "uuid": "a", "sessionId": "s",
            "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _u_text(t, **extra):
    row = {"type": "user", "uuid": "u", "sessionId": "s",
           "message": {"role": "user", "content": [{"type": "text", "text": t}]}}
    row.update(extra)
    return row


def _queued(prompt, **att_extra):
    att = {"type": "queued_command", "prompt": prompt}
    att.update(att_extra)
    return {"type": "attachment", "uuid": "q", "sessionId": "s", "attachment": att}


# 两轮历史：L4（回答二）才是"上一轮输出结束"。有这个第二回合，旧实现的缺陷才会**咬到**——
# 少一个回合就无从比较（只有一轮时两种实现都得 L2，看不出差别）。
_base = [_u_text("第一轮的问题"), _a_text("第一轮的完整回答"),
         _u_text("第二轮的问题"), _a_text("第二轮的完整回答")]


def _cut_of(rows):
    """把 rows 落盘 → 用真 adapter 读回 → 跑 locate_before_current_turn。"""
    with open(_ht_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return locate_before_current_turn(adapter, adapter.read_lines(_ht_path))


class _OldAdapter:
    """**改动前**的接口面：只有 is_user_message，没有 is_human_turn。

    两个用途：① 证明引擎的 `getattr` 兜底真的生效（第三方老 adapter 不会崩）；
    ② 当场复现缺陷——它锚到再上一轮的 user 行，切点前移一整轮。
    """
    is_user_message = adapter.is_user_message
    is_assistant_message = adapter.is_assistant_message
    get_text = adapter.get_text


# 9a. 末行是 attachment/queued_command（无 origin）⇒ 锚点落在**它之前**（L5 → 切 L4）
_q_rows = _base + [_queued("打分支，命名『测试分支』")]
_q_cut, _q_tot, _q_note = _cut_of(_q_rows)
assert (_q_cut, _q_tot) == (4, 5), f"queued_command 应被认成本轮指令，切在 L4；实为 L{_q_cut}/{_q_tot}"
assert "queued_command" in _q_note, f"note 必须点明落盘形态：{_q_note}"
assert "L5" in _q_note and "排队" in _q_note, f"note 应指锚点行并说明排队形态：{_q_note}"
# 同一份数据喂给"改动前接口"的 adapter：它锚 L3 ⇒ 切 L2，**少一整轮**（这就是被修的缺陷）
_o_cut, _, _o_note = locate_before_current_turn(_OldAdapter(), adapter.read_lines(_ht_path))
assert _o_cut == 2, f"旧接口应锚 L3 切 L2（缺陷现场），实为 L{_o_cut}"
assert "L3" in _o_note, _o_note
print(f"✓ is_human_turn(queue形态): cut=L{_q_cut}（旧接口 L{_o_cut}，少一整轮） · {_q_note}")

# 9b. 回归断言：末行是普通 type:"user" ⇒ 行为与改动前**完全一致**（L5 → 切 L4）
_u_cut, _u_tot, _u_note = _cut_of(_base + [_u_text("打分支")])
assert (_u_cut, _u_tot) == (4, 5), f"普通 user 行行为回归了：L{_u_cut}/{_u_tot}"
assert "user 消息" in _u_note and "打分支" in _u_note and "L5" in _u_note, _u_note
assert _u_cut == locate_before_current_turn(_OldAdapter(), adapter.read_lines(_ht_path))[0], \
    "普通 user 行下，新旧判据的切点必须相同"
print(f"✓ 回归(普通 user 行): cut=L{_u_cut} · {_u_note}")

# 9c. isMeta:true 的 user 行在最末 ⇒ **不**当本轮指令（锚点继续往前找到真实那条 L5）
_m_cut, _, _m_note = _cut_of(
    _base + [_u_text("打分支"), _u_text("<system-reminder>注入</system-reminder>", isMeta=True)])
assert _m_cut == 4, f"isMeta 行不该被当成本轮指令，应锚 L5 切 L4；实为 L{_m_cut}"
assert "L5" in _m_note and "打分支" in _m_note, _m_note
print(f"✓ 排除 isMeta: cut=L{_m_cut} · {_m_note}")

# 9d. toolUseResult 非空（u3 判据）的 user 行 ⇒ 同上，不是人话
_t_cut, _, _t_note = _cut_of(
    _base + [_u_text("打分支"),
             _u_text("工具输出", toolUseResult={"stdout": "ok"})])
assert _t_cut == 4, f"含 toolUseResult 的行不该被当本轮指令，应锚 L5 切 L4；实为 L{_t_cut}"
assert "L5" in _t_note and "打分支" in _t_note, _t_note
print(f"✓ 排除 toolUseResult: cut=L{_t_cut} · {_t_note}")

# 9e. queued_command 但 origin.kind 不是 human（如 task-notification）⇒ 不当人话
_n_cut, _, _n_note = _cut_of(
    _base + [_u_text("打分支"),
             _queued("任务完成通知", origin={"kind": "task-notification"})])
assert _n_cut == 4, f"origin.kind=task-notification 不该被当人话，应锚 L5 切 L4；实为 L{_n_cut}"
assert "L5" in _n_note and "打分支" in _n_note, _n_note
print(f"✓ 排除 origin.kind≠human: cut=L{_n_cut} · {_n_note}")

# 9f. 直接钉判据边界（取宽：origin 缺失/ kind=human 都算人）
assert adapter.is_human_turn(_u_text("人话")) is True
assert adapter.is_human_turn(_queued("排队指令")) is True
assert adapter.is_human_turn(_queued("排队指令", origin={"kind": "human"})) is True
assert adapter.is_human_turn(_queued("x", origin={"kind": "task-notification"})) is False
assert adapter.is_human_turn(_queued("")) is False, "取不到文本不能当锚点"
assert adapter.is_human_turn({**{"type": "attachment"}, "attachment": {"type": "file"}}) is False
assert adapter.is_human_turn(_u_text("x", turnCompanion=True)) is False
assert adapter.is_human_turn(_u_text("x", isCompactSummary=True)) is False
# 只含 tool_result 块的 user 行：不是人话（dOs 判据）
assert adapter.is_human_turn(
    {"type": "user", "message": {"role": "user",
                                 "content": [{"type": "tool_result", "tool_use_id": "t1"}]}}) is False
print("✓ is_human_turn 判据边界（取宽 · 排除 meta/u3/tool_result/非 queued attachment）")

# 9b. 贴了 request id 的那句话若是"排队形态"，也必须能被认出（2026-11-16 补）
#
# 缺口的隐蔽程度比锚点那条更高：用户贴了 id、但那句话以 attachment/queued_command 落盘，
# 而 find_pasted_request_id 只扫 type=="user" ⇒ 返回 None ⇒ 默认切点静默接管，
# 用户贴的 id 一声不响地失效（默认点常常"看着也差不多对"，所以更难发现）。
from fork_core.engine import find_pasted_request_id, apply_pasted_request_id

_RID = "aaaabbbb-1111-2222-3333-444444444444"
_pasted = '{"conversationRequestId":"%s","conversationId":"s"}  从这里打分支' % _RID

# 让 L2（第一轮的回答）身上就带被贴的那个 id —— 它才是用户指定的切点
_base_pid = [
    _u_text("第一轮的问题"),
    {"type": "assistant", "uuid": _RID, "sessionId": "s",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "第一轮的完整回答"}]}},
    _u_text("第二轮的问题"),
    _a_text("第二轮的完整回答"),
]


def _write_rows(rows):
    with open(_ht_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return adapter.read_lines(_ht_path)


# ① 正例：本轮指令是 queued_command、正文里贴了 id ⇒ 认得出、且采用它
_lines_pid = _write_rows(_base_pid + [_queued(_pasted), _a_text("本轮叙述")])
assert find_pasted_request_id(adapter, _lines_pid) == _RID, \
    "排队形态里贴的 id 没被认出（改动前这里返回 None）"
_default_pid, _, _ = locate_before_current_turn(adapter, _lines_pid)
assert _default_pid == 4, _default_pid
_new_pid, _note_pid = apply_pasted_request_id(adapter, _lines_pid, _default_pid, "s")
assert _new_pid == 2, f"应采用贴的 id 切在 L2，实为 L{_new_pid}"
assert _note_pid and "request id" in _note_pid and "L2" in _note_pid and "L4" in _note_pid, _note_pid
print(f"✓ 排队形态贴 id：默认 L{_default_pid} → 采用后 L{_new_pid}")

# ② 负例：注入行（isMeta）里贴 id，不能被当作"本轮指令"
_lines_meta_pid = _write_rows(_base_pid + [_u_text(_pasted, isMeta=True), _a_text("本轮叙述")])
assert find_pasted_request_id(adapter, _lines_meta_pid) is None, \
    "isMeta 注入行不该被当成本轮指令（放宽不能放过注入行）"
print("✓ isMeta 注入行里贴的 id 不被采纳")

shutil.rmtree(_ht_dir)

# 8. 清理
shutil.rmtree(tmpdir)
print("\n✅ Claude Code adapter 全部测试通过")

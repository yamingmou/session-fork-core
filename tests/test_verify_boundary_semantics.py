#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify 判据分层专项测试（v2.4.15）——「失败 / 需复核 / 通过」三档的边界在哪。

为什么单独一个文件：
    这套判据是**发布闸门**。它的历史教训是「宁可误报也不漏检」把闸门做成了**常驻红**：
    每正常用一次分支就出现一条 FAIL（因为在分支里记录血缘时必然写下父会话 id），
    久而久之没人再看 ⇒ 真问题被淹没。所以除了「真污染必须拦」，还必须逐条钉住
    「**什么不算失败**」，以及「放宽之后**还剩哪些硬拦**」。

覆盖：
  · 边界之外（fork 后追加）的源 id 引用 → 需复核（不判失败）
  · 前缀之内残留 / 结构性 sessionId 残留 / 边界不可信 → 仍硬失败
  · 指纹的三种证据形态：一致 / 漂移 / 缺失（旧记录）
  · 汇总口径：只有「失败」让体检变红；「需复核」不改变退出码
  · 横切守卫：**逐个产品适配器**验证 register_branch 接受并（存下 / 透传）前缀指纹

覆盖边界（如实交代，别把绿当全覆盖）：
  ✅ 已覆盖：上述判据在**本地可构造的数据**上成立；「边界后不是新回合」只写出提示、**不升级为失败**（曾加过一版从严规则，已撤，原因见 §1b）；
              CLI 的文案与退出码；workbuddy 与 claude-code 两条**真实**谱系写入路径
              （引擎读得回指纹）；7 个适配器类的**签名**与实现取值（静态，无副作用）。
  ⚠️ **已知残余风险（本文件钉住现状，不当作已解决）**：结构性检查只认 `sessionId` /
              `session_id`；若某产品用**别的字段**做会话路由，且污染落在"已开新回合的追加区"，
              本版只给需复核。升级为硬拦前必须先取证各产品的真实路由字段（盲加键名会制造
              新的常驻红）。见第 1c 节。
  ❌ 未覆盖：其余 5 个适配器的**运行时**写入（只做静态检查）——它们会写产品自己的库，
              在测试里真跑会碰真实用户数据；也未覆盖各产品**真实的追加/改写行为**
              （本用例的「追加」是我们自己构造的形态，真机行为见各 adapter 的专项测试）；
              指纹计算的 tmp/dst 异源问题在真实 SQLite 后端上未做端到端（仅逻辑核对）。
"""

import ast
import contextlib
import importlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core.engine import _prefix_fingerprint, verify_environment  # noqa: E402
from fork_core.models import SessionMeta, VerifyItem  # noqa: E402

SRC = "aaaaaaaa-1111-4000-8000-000000000001"
BRANCH = "bbbbbbbb-2222-4000-8000-000000000002"


# ============================================================
# 0. 最小假适配器：只提供 verify 需要的接口（不碰任何真实目录）
# ============================================================
class FakeAdapter:
    name = "fake"
    _RAW_KEYS = set()

    def __init__(self, lines, lineage, bad_lines=None, bad_items=None):
        self._lines = lines
        self._lineage = lineage
        #: 两种坐标**分开传**：文件行号（展示用）与条目序号（判定用）。缺省时相同。
        self._bad = list(bad_lines or [])
        self._bad_items = list(bad_items) if bad_items is not None else list(self._bad)
        self.path = "fake://branch"

    def list_all_sessions(self):
        return [(self.path, BRANCH)]

    def sort_key(self, ref):
        return 0

    def read_lines(self, ref):
        return [dict(o) for o in self._lines]

    def read_lines_checked(self, ref):
        from fork_core.adapter_base import ParseResult
        return ParseResult(objs=[dict(o) for o in self._lines],
                           bad_lines=list(self._bad), bad_items=list(self._bad_items))

    def rewrite_ids(self, lines, old_id, new_id):
        """真做递归 id 替换（空壳会让"源会话模拟替换"必然报残留，负例就失去意义）。"""
        n = 0

        def walk(node):
            nonlocal n
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v) for v in node]
            if isinstance(node, str) and old_id in node:
                n += 1
                return node.replace(old_id, new_id)
            return node

        return [walk(o) for o in lines], n

    def read_raw(self, ref):
        return "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in self._lines)

    def is_assistant_message(self, o):
        return o.get("role") == "assistant"

    def is_user_message(self, o):
        return o.get("role") == "user"

    def get_text(self, o):
        return "".join(c.get("text", "") for c in o.get("content", []) if isinstance(c, dict))

    def is_historical_id_path(self, path):
        return False

    def verify_storage(self):
        return []

    def _lineage_get(self):
        return self._lineage


def _msg(role, sid, text):
    return {"role": role, "sessionId": sid, "content": [{"text": text}]}


def _prefix():
    """fork 复制出的前缀：以完整 assistant 回复收尾（at_seq 正常的形态）。"""
    return [_msg("user", BRANCH, "问题"), _msg("assistant", BRANCH, "回答")]


def _clean_tail():
    return [_msg("user", BRANCH, "继续"), _msg("assistant", BRANCH, "收尾")]


def _fp_of(lines, n=2):
    return _prefix_fingerprint(FakeAdapter(lines, {}), "fake://branch", n)


def _verdict(lines, at_seq, prefix_fp, item_prefix="分支产物校验", bad_lines=None,
             bad_items=None):
    lineage = {BRANCH: {"parent_id": SRC, "at_seq": at_seq, "prefix_fp": prefix_fp}}
    items = verify_environment(FakeAdapter(lines, lineage, bad_lines, bad_items))
    hit = [it for it in items if it.name.startswith(item_prefix)]
    assert hit, f"未产出 {item_prefix} 项：{[i.name for i in items]}"
    return hit[0], items


# ============================================================
# 1. 边界之外的源 id 引用 → 需复核（这正是把闸门从「常驻红」救回来的那一档）
# ============================================================
_lines = _prefix() + [
    _msg("user", BRANCH, "继续"),
    _msg("assistant", BRANCH, f"我刚确认：我的父会话是 {SRC}"),
    _msg("assistant", BRANCH, "收尾"),
]
_fp = _fp_of(_lines)
assert _fp and _fp.startswith("sha256-1:"), f"指纹格式异常：{_fp}"

_item, _items = _verdict(_lines, 2, _fp)
assert _item.ok, f"边界之外的正文引用不该判失败：{_item.detail}"
assert _item.review, f"应标记需复核：{_item.detail}"
assert "需复核" in _item.detail, f"detail 应写清需复核：{_item.detail}"
assert "前缀指纹与 fork 时一致" in _item.detail, f"应给出边界证据：{_item.detail}"
# 指纹只覆盖前 N 行，不得写成"整条分支边界可信"（独立审查点名的过度承诺）
assert "仅证前" in _item.detail or "证据只覆盖前" in _item.detail, f"应写明证据范围只到前 N 行：{_item.detail}"
assert "边界可信" not in _item.detail, f"不得给出超出证据范围的安心：{_item.detail}"
print("✓ 边界之外（正文）→ 需复核，不判失败（证据范围写明，不给假安心）")

_lines_r = _prefix() + [
    _msg("user", BRANCH, "继续"),
    {"role": "assistant", "sessionId": BRANCH, "reasoning": f"读到 id={SRC} 要记下来",
     "content": [{"text": "收尾"}]},
]
_item, _ = _verdict(_lines_r, 2, _fp_of(_lines_r))
assert _item.ok and _item.review, f"思维链命中应只需复核：{_item.detail}"
assert "思维链 1 处" in _item.detail, f"思维链应单独归类计数：{_item.detail}"
print("✓ 边界之外（思维链）→ 需复核，且与正文分开计数")

# ============================================================
# 1b. 边界后不是新回合 ⇒ **只加强提示，不判失败**（v2.4.15 定案；此处记下"改过一轮"）
#     曾写过"未开新回合就从严判失败"（想关掉"污染正好写在边界后第一行"那类形态），
#     被第二轮独立审查否掉，两条理由都成立：
#       ① 轴选错：真实追加里"先开 user 回合"本来就是常态 ⇒ 它挡不住那类形态里最现实的一种
#          （边界后就是 user 消息、正文里带父 id）——那一种照样落在"追加区"；
#       ② 代价无法证伪：本机只有 WorkBuddy 有 3 条被追加过的真实分支（首行都是 user），
#          其余 5 个后端的追加首行**没有证据**是 user（Claude Code 有 compaction 的 summary
#          条目，Hermes / OpenClaw 有血缘记录条目）⇒ 一旦它们先写非 user 条目，规则会把
#          **合法内容**判成失败，等于重新制造"常驻红"——正是本版要消灭的东西。
#     ⇒ 定案：追加区一律只提示复核；"紧接边界的不是新回合"写进 detail（是信号，不足以判罪）。
# ============================================================
_lines_seam = _prefix() + [
    {"role": "assistant", "sessionId": BRANCH, "content": [{"text": f"缝处直接写 {SRC}"}]},
    _msg("assistant", BRANCH, "收尾"),
]
_item, _ = _verdict(_lines_seam, 2, _fp_of(_lines_seam))
assert _item.ok and _item.review, f"缝处引用只提示复核，不判失败：{_item.detail}"
assert "紧接边界的不是新回合" in _item.detail, f"这条信号必须写出来：{_item.detail}"

# 对照：先开了 user 回合的追加 ⇒ 同为复核，但不带那条额外提示
_lines_turn = _prefix() + [
    _msg("user", BRANCH, "继续"),
    {"role": "assistant", "sessionId": BRANCH, "content": [{"text": f"正文提到 {SRC}"}]},
    _msg("assistant", BRANCH, "收尾"),
]
_item2, _ = _verdict(_lines_turn, 2, _fp_of(_lines_turn))
assert _item2.ok and _item2.review, f"开了新回合的追加只需复核：{_item2.detail}"
assert "紧接边界的不是新回合" not in _item2.detail, "正常追加不该带那条提示"
print("✓ 追加区一律只提示复核；「边界后不是新回合」作为加强提示写出（不判失败）")

# ============================================================
# 1c. "非 sessionId 路由字段" 的处置：**取证后决定不扩张**（把决定与证据一起钉住）
#     背景：独立审查构造了"污染写在别的字段上、且落在已开新回合的追加区"的形态，
#     本版只给需复核。要决定是否扩张硬拦键名，先做了真库普查（只读）：
#       · 166 个真实 transcript / 23 万+ 次出现（只认「JSON 键 + 值含 uuid」）里，
#         承载 uuid 的键只有 sessionId（路由）、parentId / logicalParentId（**消息级**链）；
#         conversationId / threadId / sessionKey / parentUuid … 出现次数 **0**。
#       · 真实分支里"值含父会话 id"的键只有 text / value / content / arguments / reasoning
#         —— **全是内容字段**：加进硬拦 = 让"在分支里写下属血"这种正常内容重新变红
#         （13 条真实分支里 8 条会被新判红），正是本版要消灭的东西。
#     ⇒ 结论：**不扩张**（加进硬拦只会命中内容字段 ⇒ 新判红）。
#     ⚠️ **已知缺口（不当「已解决」）**：本清单只覆盖「用 sessionId 命名路由」的形态；
#        Codex 的 payload.thread_id、pi / OpenClaw 的 header.id / current_session_id 不在其中
#        ⇒ 那些后端在追加区只会给「需复核」（**偏漏检、不误报**），前缀区仍由键无关字符串搜索覆盖。
# ============================================================
from fork_core.engine import _SESSION_ID_KEYS  # noqa: E402

assert _SESSION_ID_KEYS == ("sessionId", "session_id"), \
    f"路由键清单被改动过——请先重跑 tools/routing-key-survey.py 取证（见 engine 里的普查表）：{_SESSION_ID_KEYS}"
assert os.path.isfile(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "routing-key-survey.py")), \
    "普查脚本必须随仓库提供（否则 engine 里那张表与结论无法被别人复跑核对）"

_lines_route = _prefix() + [
    _msg("user", BRANCH, "继续"),
    {"role": "assistant", "sessionId": BRANCH, "conversationId": SRC,
     "content": [{"text": "收尾"}]},
]
_item, _ = _verdict(_lines_route, 2, _fp_of(_lines_route))
assert _item.ok and _item.review, "现状：非路由键上的引用只提示复核"
assert "会话关联字段" not in _item.detail, "现状：它不触发结构性硬拦"
print("✓ 非 sessionId 路由字段：普查可复跑（166 文件 / 23 万次 + 13 条真实分支）；主键已覆盖，" 
      "其余为**已知缺口**（偏漏检、不误报），清单已钉住")

# ============================================================
# 2. 旧记录（无指纹）→ 需复核 + 明确说「无法证实」（不能假装有证据）
# ============================================================
_item, _ = _verdict(_lines, 2, None)
assert _item.ok and _item.review, f"旧记录应只提示复核：{_item.detail}"
assert "未记录前缀指纹" in _item.detail, f"应说明无法证实：{_item.detail}"
assert "建议重打该分支" in _item.detail, f"应给出可消解的动作：{_item.detail}"
print("✓ 旧记录无指纹 → 需复核 + 说明无法证实 + 给出消解动作")

# ============================================================
# 3. 硬失败面：前缀内残留 / 结构性残留——放宽之后必须仍然拦得住
# ============================================================
_lines_c = [
    _msg("user", BRANCH, "问题"),
    _msg("assistant", BRANCH, f"前缀里就写了父会话 {SRC}"),   # 落在边界之内
] + _clean_tail()
_item, _ = _verdict(_lines_c, 2, _fp_of(_lines_c))
assert not _item.ok, f"前缀内的源 id 残留必须判失败：{_item.detail}"
assert "前缀" in _item.detail, f"应指向前缀残留：{_item.detail}"
assert not _item.review, "已判失败就不必再标需复核"
print("✓ 前缀内残留 → 硬失败（负向控制）")

_lines_s = _prefix() + [
    _msg("user", BRANCH, "继续"),
    {"role": "assistant", "sessionId": SRC,                    # 结构性：会被路由回源会话
     "content": [{"text": "污染"}]},
    _msg("assistant", BRANCH, "收尾"),
]
_item, _ = _verdict(_lines_s, 2, _fp_of(_lines_s))
assert not _item.ok, f"结构性 sessionId 残留必须判失败：{_item.detail}"
assert "sessionId" in _item.detail, f"应指出是结构性残留：{_item.detail}"
print("✓ 结构性 sessionId 残留（边界之外）→ 仍硬失败（不依赖边界）")

# ============================================================
# 4. 边界不可信 → 退回宽边界 ⇒ 命中落到前缀侧 ⇒ 硬失败（从严，不是静默通过）
#    at_seq=1 指向 user 消息，不可能是「完整 assistant 回复收尾的前缀」
# ============================================================
_item, _ = _verdict(_lines, 1, _fp)
assert not _item.ok, f"at_seq 不可信时应从严（退回宽边界后命中属前缀侧）：{_item.detail}"
_item, _ = _verdict(_lines, len(_lines) + 5, _fp)
assert not _item.ok, f"at_seq 越界时应从严：{_item.detail}"
print("✓ 边界不可信（过小 / 越界）→ 退回宽边界 ⇒ 同一份内容改判硬失败（从严）")

# ============================================================
# 5. 指纹漂移 → 据实报「前缀被改动」，但不判失败（前缀内无源 id 残留）
# ============================================================
_pristine = _prefix()
_fp_orig = _fp_of(_pristine)
_drift = json.loads(json.dumps(_pristine))
_drift[1]["content"][0]["text"] = "回答（事后被改动）"
_item, _ = _verdict(_drift + _clean_tail(), 2, _fp_orig)
assert _item.ok, f"指纹漂移不该判失败（无源 id 残留）：{_item.detail}"
assert _item.review, f"指纹漂移应标记需复核：{_item.detail}"
assert "指纹与 fork 时不符" in _item.detail, f"应报出前缀被改动：{_item.detail}"
print("✓ 指纹漂移 → 需复核 + 明确报「前缀被改动」（不猜、不判失败）")

# ============================================================
# 6. 汇总口径：只有「失败」让体检变红；「需复核」不影响汇总
# ============================================================
_item, _items = _verdict(_lines, 2, _fp)
_cov = [it for it in _items if it.name == "体检覆盖"]
assert _cov, "缺体检覆盖项"
assert _cov[0].ok, f"仅需复核时汇总应通过：{_cov[0].detail}"
assert "复核" in _cov[0].detail, f"汇总文案应提示需复核：{_cov[0].detail}"
assert "无失败项" in _cov[0].detail, f"汇总应明说没有失败项：{_cov[0].detail}"
# 汇总项**不**带 review 标记：否则 CLI 的"N 项需复核"会把汇总自己算进去
# （本轮真机实测到 2 项被报成 3 项）。需复核清单在它上面的分项里。
assert not _cov[0].review, "汇总项不应带 review 标记（会把计数弄错）"

_cov2 = [it for it in _verdict(_lines_c, 2, _fp)[1] if it.name == "体检覆盖"][0]
assert not _cov2.ok and "失败" in _cov2.detail, f"有硬失败时汇总应变红：{_cov2.detail}"
print("✓ 汇总口径：需复核 ⇒ 通过（不阻塞）；有失败 ⇒ 变红")

# ============================================================
# 7. CLI 契约：文案与退出码（用户看到的就是这两个东西）
# ============================================================
import fork_core.cli as cli_mod        # noqa: E402
import fork_core.engine as engine_mod  # noqa: E402


def _run_cli(items):
    orig = engine_mod.verify_environment
    engine_mod.verify_environment = lambda a: items
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf):
            try:
                cli_mod.run_verify(FakeAdapter([], {}))
            except SystemExit as e:
                code = e.code
    finally:
        engine_mod.verify_environment = orig
    return buf.getvalue(), code


_out, _code = _run_cli([VerifyItem("分支产物校验 abc", "L2", True, "零残留（需复核 1 处）", review=True)])
assert _code == 0, f"仅需复核不该非 0 退出（实际 {_code}）"
assert "⚠️" in _out and "需人工复核" in _out, f"应显式标注需复核：{_out}"
assert "存在失败项" not in _out, f"不该说存在失败：{_out}"

_out, _code = _run_cli([VerifyItem("分支产物校验 abc", "L2", False, "源 id 残留")])
assert _code == 1, f"有失败项应以 1 退出（实际 {_code}）"
assert "存在失败项" in _out and "请修复" in _out, f"失败文案应保留：{_out}"

_out, _code = _run_cli([VerifyItem("数据库", "L2", True, "ok")])
assert _code == 0, f"全绿应退出 0（实际 {_code}）"
assert "全部通过" in _out, f"无失败无复核应报全部通过：{_out}"
print("✓ CLI 契约：需复核 → 退出 0 + ⚠️ 文案；有失败 → 退出 1 + 请修复")

# ============================================================
# 8. 横切守卫：逐个适配器检查「接受 + 存下 / 透传」前缀指纹
#    历史缺陷形态：新增横切能力时漏掉某个适配器（曾有 Hermes 漏覆盖 backups_dir()）
# ============================================================
_ADAPTERS = [
    ("adapter_workbuddy", "WorkBuddyAdapter", "delegate"),
    ("adapter_claude_code", "ClaudeCodeAdapter", "store"),
    ("adapter_codex", "CodexAdapter", "store"),
    ("adapter_pi", "PiAdapter", "store"),
    ("adapter_hermes", "HermesAdapter", "store"),
    ("adapter_openclaw", "OpenClawJsonlAdapter", "pass"),
    ("adapter_openclaw_sqlite", "OpenClawSqliteAdapter", "pass"),
]

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for _modname, _clsname, _mode in _ADAPTERS:
    _cls = getattr(importlib.import_module(f"fork_core.{_modname}"), _clsname)

    # 8a. 签名：必须接受 prefix_fp（否则引擎会静默退回旧调用，该产品少一层证据）
    _params = inspect.signature(_cls.register_branch).parameters
    _accepts = "prefix_fp" in _params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in _params.values())
    assert _accepts, f"{_clsname}.register_branch 不接受 prefix_fp ⇒ 该产品的分支拿不到指纹"

    # 8b. 实现：必须真把它落到谱系记录上（store / delegate），或透传给父类（pass）
    _src = open(os.path.join(_root, "fork_core", f"{_modname}.py"), encoding="utf-8").read()
    _fn = ""
    for _node in ast.walk(ast.parse(_src)):
        if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _node.name == "register_branch":
            _fn = ast.get_source_segment(_src, _node) or ""
            break
    assert _fn, f"{_modname} 未找到 register_branch 实现"
    if _mode == "store":
        assert '"prefix_fp"' in _fn, f"{_clsname} 收了参数却没写进谱系记录（会静默丢失）"
        _note = "写入记录 ✅"
    elif _mode == "delegate":
        # 记录由同模块的写入助手构造（如 workbuddy 的 _lineage_add）：
        # 要求 register_branch 真把它交出去，且模块里确有一处把它写进记录
        assert "prefix_fp" in _fn, f"{_clsname} 没把 prefix_fp 交给写入助手"
        assert '"prefix_fp"' in _src, f"{_clsname} 模块内没有把 prefix_fp 写进记录的地方（会静默丢失）"
        _note = "交给写入助手 ✅"
    else:
        assert "prefix_fp=prefix_fp" in _fn, f"{_clsname} 没把 prefix_fp 透传给父类"
        _note = "透传父类 ✅"
    print(f"  · {_clsname:<22} 接受 ✅   {_note}")

print("✓ 横切守卫：7 个适配器类全部接受，且写入/透传均有据（静态；运行时证据见下）")

# ============================================================
# 9. 运行时证据：两条**真实**谱系写入路径（引擎要读的两种记录形态都验一遍）
#    workbuddy = _lineage_get() 的 {id: {...}} 形态
#    claude    = _read_index() 的 {"branches": [...]} 形态
#    （都不碰真实用户目录：把路径常量指到临时目录）
# ============================================================
_tmp = tempfile.mkdtemp(prefix="fork-fp-test-")
try:
    import fork_core.adapter_workbuddy as wb_mod
    import fork_core.adapter_claude_code as cl_mod

    # 9a. workbuddy：走真实谱系写入；db 部分用 mock 吸收（不碰真实库）
    _wb = wb_mod.WorkBuddyAdapter()
    _wb_path = os.path.join(_tmp, "wb-fork.lineage.json")
    with mock.patch.object(wb_mod, "LINEAGE_PATH", _wb_path), \
         mock.patch.object(_wb, "_connect", mock.MagicMock()):
        _wb.register_branch(SessionMeta(id=SRC, cwd="/tmp/ws"), BRANCH,
                            os.path.join(_tmp, "b.jsonl"), "分支名",
                            parent_id=SRC, at_seq=2, prefix_fp=_fp)
    _rec = json.load(open(_wb_path, encoding="utf-8"))["forks"][-1]
    assert _rec.get("prefix_fp") == _fp, f"workbuddy 谱系未存下指纹：{_rec}"
    assert _rec.get("at_seq") == 2, "同一条记录应同时保留 at_seq"

    # 9b. claude-code：BRANCH_INDEX / CLAUDE_DIR 一起指到临时目录（其写前路径断言会核对两者）
    _cl_path = os.path.join(_tmp, "fork.branches.json")
    with mock.patch.object(cl_mod, "BRANCH_INDEX", _cl_path), \
         mock.patch.object(cl_mod, "CLAUDE_DIR", _tmp):
        cl_mod.ClaudeCodeAdapter().register_branch(
            SessionMeta(id=SRC), BRANCH, os.path.join(_tmp, "b.jsonl"), "分支名",
            parent_id=SRC, at_seq=2, prefix_fp=_fp)
    _rec2 = json.load(open(_cl_path, encoding="utf-8"))["branches"][-1]
    assert _rec2.get("prefix_fp") == _fp, f"claude 索引未存下指纹：{_rec2}"
finally:
    shutil.rmtree(_tmp, ignore_errors=True)
print("✓ 运行时证据：workbuddy（{id:{...}}）与 claude-code（branches 列表）"
      "都真把指纹落进谱系，引擎读得回")

# ============================================================
# 10. 坏行：**不抛、也不静默**（v2.4.15 补）
#     修的是独立审查点名的形态：一个坏行过去让**整轮体检崩掉**，其余检查全被跳过，
#     而报错理由还指向"检查器"而不是"哪一行坏了"。
#     ⚠️ 证据等级：本机真库 165 个真实会话**实测 0 例坏行** ⇒ 属**防御性**加固；
#        这里用构造数据验证（我们不假装它已经在真机上发生过）。
# ============================================================
from fork_core.adapter_base import parse_jsonl, read_jsonl_head  # noqa: E402

_r = parse_jsonl('{"a":1}\n\n  \n{"b":2}\n这不是JSON\n[1,2]\n')
assert len(_r.objs) == 2, f"坏行/非对象应被跳过：{_r.objs}"
assert _r.bad_lines == [5, 6], f"坏行行号应为 [5,6]（空行不算坏、非对象也算坏）：{_r.bad_lines}"

# ① 坏行落在**追加区** ⇒ 只需复核，且体检整体不因此崩
_lines_bt = _prefix() + [_msg("user", BRANCH, "继续"), _msg("assistant", BRANCH, "收尾")]
_item, _items = _verdict(_lines_bt, 2, _fp_of(_lines_bt), bad_lines=[4])
assert _item.ok, f"追加区的坏行不该判失败：{_item.detail}"
assert "无法解析" in _item.detail, f"坏行必须被说出来（不许静默跳过）：{_item.detail}"
assert [it for it in _items if it.name == "体检覆盖"][0].ok, "坏行不该让体检整体变红"

# ② 坏行落在**快照点之内** ⇒ 解析后的条目数与行号已错位 ⇒ 弃用边界、从严
_lines_bp = _prefix() + [
    _msg("user", BRANCH, "继续"),
    _msg("assistant", BRANCH, f"正文提到 {SRC}"),
    _msg("assistant", BRANCH, "收尾"),
]
_item, _ = _verdict(_lines_bp, 2, _fp_of(_lines_bp), bad_lines=[1])
assert not _item.ok, f"坏行落在快照点内 ⇒ 应弃用边界并从严：{_item.detail}"

# ③ 无快照点（旧记录）+ 坏行 ⇒ 明确报"哪几行坏了"，而不是泛泛一句残留
_item, _ = _verdict(_lines_bp, None, None, bad_lines=[2])
assert not _item.ok, f"应判失败：{_item.detail}"
assert "无法解析" in _item.detail and "第 [2]" in _item.detail, f"应点名坏行：{_item.detail}"

# ④ 源会话的坏行：不算我们的缺陷，但要说清"在复制范围内 ⇒ 打分支会被拒绝"
_src_adapter = FakeAdapter(_prefix() * 2, {})   # 无谱系 ⇒ 全按源会话检查
_src_items = verify_environment(_src_adapter)
assert all(it.ok for it in _src_items if it.name.startswith("真实数据替换")), "源会话应通过（只跳过坏行）"
_src_items_bad = verify_environment(FakeAdapter(_prefix() * 2, {}, bad_lines=[1]))
_src_hit = [it for it in _src_items_bad if it.name.startswith("真实数据替换")][0]
assert _src_hit.ok and _src_hit.review, f"源会话坏行应只提示复核：{_src_hit.detail}"
assert "无法解析" in _src_hit.detail, f"源会话坏行也须报出：{_src_hit.detail}"

# ⑤ 坐标分离（第三轮审查要求补；**必须构成能区分的形状**）：
#    坏行「文件第 5 行」但只是「第 2 个条目」，而 at_seq=2。按**条目序号**判 ⇒ 它落在前缀内
#    ⇒ 硬失败；若错用文件行号（5 > 2）⇒ 会被当成"追加区的坏行"⇒ 只提示、判通过（假绿）。
#    （这是唯一能区分两种坐标的判据点：用别处会先被"前缀坏行"兜住，反而测不出坐标错用。）
_lines_clean_tail = _prefix() + [_msg("user", BRANCH, "继续"), _msg("assistant", BRANCH, "收尾")]
_it_ord, _ = _verdict(_lines_clean_tail, 2, _fp_of(_lines_clean_tail),
                      bad_lines=[5], bad_items=[2])
assert not _it_ord.ok, f"坏行按条目序号落在前缀内 ⇒ 应硬失败：{_it_ord.detail}"
assert "第 [5]" in _it_ord.detail, f"报给用户的应是文件行号：{_it_ord.detail}"

# ⑥ 流式读头（② 省内存）必须与"全量读 + 取前 n 个非空行"**逐字节等价**
_head_file = os.path.join(tempfile.mkdtemp(prefix="fork-head-"), "h.jsonl")
with open(_head_file, "w", encoding="utf-8") as f:
    f.write('{"l":1}\n\n{"l":2}\n   \n{"l":3}\n{"l":4}\n')
_full = [l for l in open(_head_file, encoding="utf-8").read().splitlines() if l.strip()]
assert read_jsonl_head(_head_file, 3).splitlines() == _full[:3], "流式读头与全量读的前 3 行不一致"
assert read_jsonl_head("/nonexistent/path", 3) is None, "非文件引用应返回 None（调用方回退全量读）"
# 指纹两侧一致性：用头部读出的内容算指纹 == 用全量读出的前 n 行算指纹
_a_head = FakeAdapter(_prefix() * 3, {})
_a_head.read_raw_head = lambda ref, n: "".join(
    json.dumps(o, ensure_ascii=False) + "\n" for o in _a_head._lines[:n])
assert _prefix_fingerprint(_a_head, "fake://branch", 2) == _fp_of(_prefix() * 3), \
    "流式读头算出的指纹与全量读不一致 ⇒ 会凭空报'漂移'"

import shutil as _shutil  # noqa: E402
_shutil.rmtree(os.path.dirname(_head_file), ignore_errors=True)
print("✓ 坏行：不抛不静默（追加区→复核 / 快照点内→从严 / 源会话→提示并说明后果）；"
      "流式读头与全量读逐字节等价")

# ============================================================
# 11. 运行期证据（补齐）：其余 5 个适配器也**真把指纹落进自己的谱系**
#     此前这 5 个只做了静态检查（"源码里有取值"），现在逐个真调一次 register_branch，
#     把产品侧的写入全部打桩到临时目录/替身，不碰真实用户数据。
# ============================================================
_tmp2 = tempfile.mkdtemp(prefix="fork-fp-dyn-")
try:
    import fork_core.adapter_codex as cx_mod            # noqa: E402
    import fork_core.adapter_pi as pi_mod               # noqa: E402
    import fork_core.adapter_hermes as hm_mod           # noqa: E402
    import fork_core.adapter_openclaw as oc_mod         # noqa: E402
    import fork_core.adapter_openclaw_sqlite as ocs_mod  # noqa: E402

    _art = os.path.join(_tmp2, "artifact.jsonl")
    with open(_art, "w", encoding="utf-8") as _f:
        _f.write('{"x":1}\n')
    _src = SessionMeta(id=SRC, created_at=0)

    def _fp_in(lineage_path):
        d = json.load(open(lineage_path, encoding="utf-8"))
        recs = d.get("branches") or d.get("forks") or []
        assert recs, f"谱系为空：{lineage_path}"
        return recs[-1].get("prefix_fp")

    # pi：只写旁路谱系
    _a = pi_mod.PiAdapter()
    _p = os.path.join(_tmp2, "pi.json")
    with mock.patch.object(_a, "LINEAGE_PATH", _p):
        _a.register_branch(_src, BRANCH, _art, "分支名", parent_id=SRC, at_seq=1, prefix_fp=_fp)
    assert _fp_in(_p) == _fp, "pi 未落盘指纹"

    # hermes：只写旁路谱系（name 传空以跳过产品标题写库）
    _a = hm_mod.HermesAdapter()
    _p = os.path.join(_tmp2, "hermes.json")
    with mock.patch.object(_a, "LINEAGE_PATH", _p):
        _a.register_branch(_src, BRANCH, _art, "", parent_id=SRC, at_seq=1, prefix_fp=_fp)
    assert _fp_in(_p) == _fp, "hermes 未落盘指纹"

    # codex：两个产品库 + 旁路谱系（库写入用替身吸收）
    _a = cx_mod.CodexAdapter()
    _p = os.path.join(_tmp2, "codex.json")
    with mock.patch.object(_a, "LINEAGE_PATH", _p), \
         mock.patch.object(_a, "_connect", mock.MagicMock()), \
         mock.patch.object(_a, "_thread_rows", mock.MagicMock(return_value=[])):
        _a.register_branch(_src, BRANCH, _art, "分支名", parent_id=SRC, at_seq=1, prefix_fp=_fp)
    assert _fp_in(_p) == _fp, "codex 未落盘指纹"

    # openclaw（JSONL 介质）：sessions.json 打桩 + 旁路谱系
    _a = oc_mod.OpenClawJsonlAdapter()
    _p = os.path.join(_tmp2, "oc.json")
    with mock.patch.object(_a, "LINEAGE_PATH", _p), \
         mock.patch.object(_a, "_read_sessions_index", lambda *a, **k: {}), \
         mock.patch.object(_a, "_write_sessions_index", lambda *a, **k: None), \
         mock.patch.object(_a, "_src_index_entry", lambda *a, **k: {}):
        _a.register_branch(_src, BRANCH, _art, "分支名", parent_id=SRC, at_seq=1, prefix_fp=_fp)
    assert _fp_in(_p) == _fp, "openclaw(JSONL) 未落盘指纹"

    # openclaw（SQLite 介质）：产品行随 finalize 落地，register 只写旁路谱系
    _a = ocs_mod.OpenClawSqliteAdapter()
    _p = os.path.join(_tmp2, "ocs.json")
    with mock.patch.object(_a, "LINEAGE_PATH", _p):
        _a.register_branch(_src, BRANCH, _art, "分支名", parent_id=SRC, at_seq=1, prefix_fp=_fp)
    assert _fp_in(_p) == _fp, "openclaw(SQLite) 未落盘指纹"
finally:
    shutil.rmtree(_tmp2, ignore_errors=True)
print("✓ 运行期证据（补齐）：pi / hermes / codex / openclaw(JSONL) / openclaw(SQLite) "
      "五个适配器都真把 prefix_fp 落进各自谱系")

print("\n✅ verify 判据分层（v2.4.15）全部通过：需复核可消解、硬失败面仍在")

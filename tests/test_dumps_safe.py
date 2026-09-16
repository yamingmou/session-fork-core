#!/usr/bin/env python3
"""dumps_safe —— 孤立代理不再导致写出崩溃（序列化安全阀的**分类对照**测试）。

为什么需要本文件
----------------
`json.dumps(o, ensure_ascii=False)` 的产物若含**孤立代理**（lone surrogate，U+D800–U+DFFF），
写出时（`open(..., encoding="utf-8")` 的 write，或 sqlite3 绑定 str）会抛
`UnicodeEncodeError: ... surrogates not allowed` ⇒ **整份分支产物写不出来**。
首个实测现场：WorkBuddy 会话 `ec48e1ae-*.jsonl` 第 18968 行（全库 158 文件 / 238,636 行中唯一一处）。

**防假绿是本文件的重点**——"修好了"的反面证据必须同样钉住：
  ① 现状写法（`ensure_ascii=False` + utf-8）对含代理对象**必须抛** → 否则测试没碰到那个分支（空转）；
  ② `surrogatepass` 产物**必须不是**合法 UTF-8（不采用它的理由）；
  ③ **正例对照**：合法代理对（真 emoji）**必须不降级**，保持原形态 → 否则"修好孤儿、弄坏正常 emoji"。

【条款 24 已审 · 2026-09-16】断言期望值说明：
  降级后（`ensure_ascii=True`）的产物**仍是同一个孤立代理**——孤立代理在 JSON 层是**合法转义**，
  `json.loads` 回读一致。故判据是「**回读语义逐字相同**」，**不是**「文本里没有代理」：
  后者等价于**净化**（替换为 U+FFFD 或丢弃），会毁掉"零改动复制工作现场"这一契约，已明令禁止。

纪律：**只读用户真实数据**（真实负例仅读取，绝不写入）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core.adapter_base import dumps_safe  # noqa: E402
from fork_core.adapter_workbuddy import WorkBuddyAdapter  # noqa: E402

LONE_HIGH = "U+D83D"
LONE_LOW = "U+DE00"
EMOJI = "U+1F600"


def lone(hexcp):
    """由码点构造孤立代理字符（不写字面转义，避免源码自身带代理）。"""
    return chr(int(hexcp[2:], 16))


# ----------------------------------------------------------------------
# 一、合成负例：孤高 / 孤低 —— 必须能安全写出，且回读一致
# ----------------------------------------------------------------------
for label, cp in (("孤高 " + LONE_HIGH, LONE_HIGH), ("孤低 " + LONE_LOW, LONE_LOW)):
    obj = {"text": "前缀" + lone(cp) + "后缀", "n": 7}
    text = dumps_safe(obj)
    text.encode("utf-8")                      # 不抛 = 可安全写出
    back = json.loads(text)
    assert back == obj, label + " 回读必须逐字一致"
    assert back["text"][2] == lone(cp), label + " 代理必须原样保留（不净化、不丢弃）"
print("✓ 合成负例：孤高/孤低均可安全序列化，且回读逐字一致（不净化）")

# ----------------------------------------------------------------------
# 二、正例对照（防"修好孤儿、弄坏正常 emoji"）：合法 emoji 必须**不降级**
# ----------------------------------------------------------------------
obj = {"text": "😀ok", "n": 1}
got = dumps_safe(obj)
exp = json.dumps(obj, ensure_ascii=False)
assert got == exp, "合法 emoji 不得触发降级（产物应与 ensure_ascii=False 完全一致）"
assert "😀" in got, "合法 emoji 应保持原字符形态（人类可读）"
assert chr(0xFFFD) not in got, "不得出现 U+FFFD（净化）"
print("✓ 正例对照：合法 emoji 不降级、保持原形态、无 U+FFFD")

# ----------------------------------------------------------------------
# 三、反例对照（防假绿）：现状写法必须炸；surrogatepass 必须非法
# ----------------------------------------------------------------------
bad = {"text": lone(LONE_HIGH)}
raised = False
try:
    json.dumps(bad, ensure_ascii=False).encode("utf-8")
except UnicodeEncodeError:
    raised = True
assert raised, "现状写法对孤立代理必须抛 UnicodeEncodeError——否则本测试没覆盖到故障分支（空转）"

cesu = json.dumps(bad, ensure_ascii=False).encode("utf-8", "surrogatepass")
illegal = False
try:
    cesu.decode("utf-8")
except UnicodeDecodeError:
    illegal = True
assert illegal, "surrogatepass 产物必须不是合法 UTF-8（宿主严格读会吞字）⇒ 故不采用"
print("✓ 反例对照：现状写法必炸、surrogatepass 产物非法 UTF-8（两条不采用的理由均成立）")

# ----------------------------------------------------------------------
# 四、真实负例（活会话 → 只做**不变量**断言，不写死行号）
#     依据 tests/regress_fossils.py 的纪律：活会话每轮都在长，写死精确值必然每跑必红。
# ----------------------------------------------------------------------
PROJ = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects",
                    "Users-maxwell-WorkBuddy-2026-08-20-19-07-45")
real = None
if os.path.isdir(PROJ):
    for name in sorted(os.listdir(PROJ)):
        if name.startswith("ec48e1ae") and name.endswith(".jsonl"):
            real = os.path.join(PROJ, name)
            break

if not real:
    print("⏭ 真实负例跳过：样本会话不存在（不 fail——属可接受状态）")
else:
    hits = []
    with open(real, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue

            def find(x, acc):
                if isinstance(x, str):
                    for j, c in enumerate(x):
                        if 0xD800 <= ord(c) <= 0xDFFF:
                            acc.append(j)
                            break
                elif isinstance(x, dict):
                    for v in x.values():
                        find(v, acc)
                elif isinstance(x, list):
                    for v in x:
                        find(v, acc)

            acc = []
            find(o, acc)
            if acc:
                hits.append((i, line, o, acc[0]))
    if not hits:
        print("⏭ 真实负例跳过：该会话当前已无孤立代理（不 fail——修数据与修工具是两件事）")
    else:
        line_no, line, obj_real, pos = hits[0]
        text = dumps_safe(obj_real)
        text.encode("utf-8")
        back = json.loads(text)
        assert back == obj_real, "真实负例回读必须逐字一致"
        acc2 = []
        find(back, acc2)
        assert acc2 == [pos], "真实负例的代理位置必须保持一致（%d vs %d）" % (pos, acc2[0])
        print("✓ 真实负例：行 %d（代理位 %d）可安全序列化且回读逐字一致" % (line_no, pos))

# ----------------------------------------------------------------------
# 五、端到端：走真实 adapter 的 write_branch（故障原始现场就在这一行）
# ----------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="dumps-safe-")
try:
    dst = os.path.join(tmp, "branch.jsonl")
    lines = [
        {"type": "message", "role": "user",
         "content": [{"type": "text", "text": "载体 " + lone(LONE_HIGH)}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "text", "text": "ok 😀"}]},
    ]
    WorkBuddyAdapter().write_branch(dst, lines)      # 故障原现场：此前在此抛 UnicodeEncodeError
    with open(dst, encoding="utf-8") as fh:
        back = [json.loads(l) for l in fh if l.strip()]
    assert back == lines, "write_branch 产物回读必须逐字一致"
    assert chr(0xFFFD) not in open(dst, encoding="utf-8").read(), "产物不得含 U+FFFD"
    print("✓ 端到端：WorkBuddyAdapter.write_branch 对含孤立代理的行成功写出且逐字一致")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print("\n✅ dumps_safe 测试通过（合成负例 + 正例对照 + 反例对照 + 真实负例 + 端到端）")

#!/usr/bin/env python3
"""WorkBuddy 适配器 · `--session current` 解析测试（2026-09-15 事故后补的第一块覆盖）。

为什么单独有这份文件
--------------------
本适配器是**主战场（用户日常就住在这个产品里）**，此前 tests/ 下只有
claude / codex / hermes / pi / openclaw 的适配器测试，**WorkBuddy 零覆盖**——
所以一条"会打到另一个对话"的解析路径可以一路全绿地活到线上。

事故形态（2026-09-15 实测）
--------------------------
bug 线里先后两次执行**同一条** `--session current`：
  19:35 → 父 = 本对话（对）
  19:43 → 父 = 「检索与审核」（错，另一个对话的分支）
根因：拿不到执行环境会话标识时，静默退回
      `SELECT id FROM sessions WHERE status='working' ORDER BY created_at DESC LIMIT 1`
      ——"最新的 working 会话"与"我正在其中的这个对话"根本不是一回事。

修法：`current` 不猜。无标识 / 标识无对应文件 → 硬失败；旧语义挪到显式入口
      `--session latest-working`。

本文件把这些判据钉住，防止再退回去。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fork_core import adapter_workbuddy as M  # noqa: E402
from fork_core.adapter_workbuddy import WorkBuddyAdapter  # noqa: E402

DDL = """
CREATE TABLE sessions (
    id                       TEXT PRIMARY KEY,
    cwd                      TEXT NOT NULL,
    user_id                  TEXT,
    title                    TEXT,
    custom_title             TEXT,
    status                   TEXT NOT NULL,
    created_at               INTEGER,
    updated_at               INTEGER,
    deleted_at               INTEGER,
    is_playground            INTEGER NOT NULL,
    source_mode              TEXT,
    is_background_automation INTEGER
)
"""

WS = "ws-1"
# A = 更晚创建的 working 会话（旧回退会挑中它）；B = 本对话，已完成，只它有不转录
SID_A = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
SID_B = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"
SID_GHOST = "cccccccc-3333-3333-3333-cccccccccccc"  # 有标识、无文件
# ⚠️ SID_C 是**更早**的 working 会话，且**先于** SID_A 插入。
#    没有它，库里就只有一个 working 行 ⇒ `ORDER BY created_at DESC` 从来没被真正测到
#    （独立审查 2026-09-15 抓出的盲区：去掉排序这个用例照样绿）。
SID_C = "dddddddd-4444-4444-4444-dddddddddddd"


class WorkBuddyResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        M.PROJECTS_DIR = os.path.join(root, "projects")
        M.DB_PATH = os.path.join(root, "workbuddy.db")
        M.LINEAGE_PATH = os.path.join(root, "fork.lineage.json")
        os.makedirs(os.path.join(M.PROJECTS_DIR, WS))

        db = sqlite3.connect(M.DB_PATH)
        db.execute(DDL)
        # 先插更早的 working（SID_C），再插更新的 working（SID_A）——顺序本身也是判据的一部分
        db.execute(
            "INSERT INTO sessions (id,cwd,user_id,title,custom_title,status,created_at,"
            "updated_at,deleted_at,is_playground,source_mode,is_background_automation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (SID_C, "/tmp", "u", "更早的对话", None, "working", 500, 500, None, 0, None, None),
        )
        db.execute(
            "INSERT INTO sessions (id,cwd,user_id,title,custom_title,status,created_at,"
            "updated_at,deleted_at,is_playground,source_mode,is_background_automation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (SID_A, "/tmp", "u", "别的对话", None, "working", 2000, 2000, None, 0, None, None),
        )
        db.execute(
            "INSERT INTO sessions (id,cwd,user_id,title,custom_title,status,created_at,"
            "updated_at,deleted_at,is_playground,source_mode,is_background_automation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (SID_B, "/tmp", "u", "本对话", "本对话·自定义名", "completed", 1000, 1000, None, 0, None, None),
        )
        db.commit()
        db.close()

        # 只为 B 造一份转录文件（A 有 working 状态但无文件）
        with open(os.path.join(M.PROJECTS_DIR, WS, SID_B + ".jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}\n')

        self._env_backup = {k: os.environ.get(k) for k in
                            ("CLAUDE_SESSION_ID", "CODEBUDDY_SESSION_ID", "BAGGAGE")}
        for k in self._env_backup:
            os.environ.pop(k, None)

        self.adapter = WorkBuddyAdapter()  # 实例化时快照上面的模块级路径

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # ---- 判据 1：显式 id 原样返回 ----
    def test_explicit_id_passthrough(self):
        self.assertEqual(self.adapter.resolve_session(SID_B), SID_B)

    # ---- 判据 2：有标识 → 命中该对话，且不因 SQL 里"更新的 working 会话"而改主意 ----
    def test_current_uses_env_marker_even_if_newer_working_exists(self):
        os.environ["CLAUDE_SESSION_ID"] = SID_B
        self.assertEqual(self.adapter.resolve_session("current"), SID_B)

    def test_current_uses_baggage_marker(self):
        os.environ["BAGGAGE"] = "codebuddy.session_id=" + SID_B + ",other=x"
        self.assertEqual(self.adapter.resolve_session("current"), SID_B)

    # ---- 判据 3：无标识 → 硬失败（本次事故的回归闸）----
    def test_current_without_marker_must_fail_not_guess(self):
        with self.assertRaises(SystemExit) as cm:
            self.adapter.resolve_session("current")
        msg = str(cm.exception)
        self.assertIn("无法确定", msg)
        # 必须**不能**落到别的对话上
        self.assertNotIn(SID_A, msg)

    # ---- 判据 4：标识存在但无对应文件 → 也不猜 ----
    def test_current_marker_without_transcript_must_fail(self):
        os.environ["CLAUDE_SESSION_ID"] = SID_GHOST
        with self.assertRaises(SystemExit) as cm:
            self.adapter.resolve_session("current")
        self.assertIn("找不到对应的对话文件", str(cm.exception))

    # ---- 判据 5：旧语义有显式入口，且挑的就是"最新 working"（不再冒充 current）----
    def test_latest_working_is_explicit_and_picks_newest_working(self):
        # 库里有两个 working：SID_C(500 更早) 与 SID_A(2000 更晚)。必须挑**更晚**的那个。
        got = self.adapter.resolve_session("latest-working")
        self.assertEqual(got, SID_A)
        self.assertNotEqual(got, SID_C, "挑到了更早的 working ⇒ 排序没生效")

    def test_latest_working_without_working_session_fails(self):
        db = sqlite3.connect(M.DB_PATH)
        db.execute("UPDATE sessions SET status='completed'")
        db.commit()
        db.close()
        with self.assertRaises(SystemExit):
            self.adapter.resolve_session("latest-working")

    # ---- 判据 6：输出要能回显源对话名字（让打错无处藏身）----
    def test_describe_session_returns_display_name(self):
        self.assertEqual(self.adapter.describe_session(SID_B), "本对话·自定义名")
        self.assertEqual(self.adapter.describe_session(SID_A), "别的对话")
        self.assertEqual(self.adapter.describe_session("nonexistent"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

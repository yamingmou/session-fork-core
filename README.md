<h1><img src="https://raw.githubusercontent.com/yamingmou/session-fork-core/main/logo.png" width="40" height="40" alt="Fork Logo" style="vertical-align: middle;"> Session Fork（会话分叉 · 打分支）</h1>

[English](README.en.md) | 简体中文

[![Version](https://img.shields.io/github/v/release/yamingmou/session-fork-core?label=version)](https://github.com/yamingmou/session-fork-core/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Platforms](https://img.shields.io/badge/platforms-WorkBuddy%20%7C%20Claude%20Code%20%7C%20Codex%20%7C%20Hermes%20%7C%20pi%20%7C%20OpenClaw-blue.svg)
[![Python](https://img.shields.io/badge/python-3.9%2B-informational.svg)](https://www.python.org/)

**走到一半想换个方向，又不想把前面重来一遍？**

Session Fork 把当前这段工作**整体复制**出一个独立分支——上下文、已经确认的结论、做过的步骤、工具跑出来的结果，全都跟着走。你从任意一个节点接着往下推进，原线照常继续，两边互不干扰。

**不用重讲一遍背景，不用重跑一遍前面的步骤，也不用担心把原线带偏。**

分叉的从来不只是一段对话：**任务、方案、代码、写作、调研**——凡是"推进到一半、带着上下文和已有结论"的过程，都能分叉。

- 「这条方向走岔了，回到上一轮重新来」→ 打一条分支，从那里重走；
- 「同一个任务，想并行试两三条路」→ 各开一条平行线分别展开，最后挑一条；
- 「前半段已经定了，别动它」→ 分支是独立副本（不是链接指向），原线后续怎么变都不会串进来。

- 当前支持（**6 个产品，全部真机实测**）：**WorkBuddy**（默认，真库实测）· **Claude Code**（真机实测）· **Codex**（真机实测）· **Hermes**（真机实测）· **pi**（真机实测）· **OpenClaw**（真机实测，两个世代都支持）
- OpenClaw 两种世代已分别验证：`≤2026.6.x`（JSONL）与 `≥2026.9.x`（SQLite 的 `transcript_events` 表，与 JSONL 同构）——引擎产出的分支都被 OpenClaw 自己列出并能继续对话
- 不在范围：本地不留转录的云优先服务
- 作者：OfferKuai（Offer快）团队 · License：MIT
- 版本：**以顶部 Release 徽章为准**（此处不手写——手写必与 tag 漂移）
- **变更历史**：见 [CHANGELOG.md](CHANGELOG.md)——每版按「价值 / 实现 / 修改 / 检查」四段写，"检查"给的是**你可以自己复核**的方式

## 分叉的是什么——以及不是什么

分叉复制的是**会话的上下文**，它不会把外部世界一起回退。这是使用前唯一值得先读清楚的边界。

| 产物形态 | 分叉后能否回到那一刻 |
|---|---|
| 在 git 仓库里（已提交） | ✅ 能。把工作区指到对应提交（`git switch -c <名> <sha>` / `git worktree add <目录> <sha>`），上下文与产物重新对齐 |
| 有版本记录的载体（云文档版本历史、系统快照 / Time Machine、备份、日志） | ✅ 通常能。手动回溯到对应时间点即可 |
| 无任何版本记录的本地文件 | ❌ 不能。它**只有最终态**存在。分叉给到你的是「旧上下文 + 新文件」 |
| 已经发生的外部副作用（发出的消息 / 邮件、已发布页面、已 push 的提交、API 写入、装好的依赖） | ❌ 不可撤销。分叉不能取消已经发生的事 |

另外两点值得知道：

- 跟着分支走的是工具结果的**记录文本**（会话里存下来的那段），**不是**把工具重新跑一遍；
- 分支是**快照**：分叉点之后原会话新增的消息不会出现在分支里。这是设计行为，不是丢数据。

**一条实用准则**：想让分支**真的回到过去**，先给产物打检查点——`git commit`（或 `stash` / `tag` / `worktree`）。若产物完全没有版本记录、而你又可能需要旧版本，请在分叉前先备份。

## 功能特性

- **零配置**：说一句"打分支"就完事，默认在上一轮输出结束处切开，不用你去找断点；
- **断点你说了算**：想从哪条回复分，就按那条回复切（特征文本 / 请求ID / 行号）；
- **原线保持干净**：分支是独立的一份真文件（不是链接指向），原会话后续怎么聊都不会串进分支；
- **谱系看得见**：每个分支都记着从哪来，`--list --tree` 一眼看完整棵分叉树；
- **能一直分下去**：从分支还能再打分支，分叉的分叉；
- **动手前先备份、先预览**：执行前自动备份源会话，`--dry-run` 可先确认切在哪。

## 安装

**先看你在用哪个产品**（上面列了 6 个），再选对应的一条。**三种入口的参数完全一致——唯一例外：非 WorkBuddy 用户必须补 `--adapter <你的产品>`。**

| 你在用 | 怎么装 | 装完在哪 | 怎么用 |
|---|---|---|---|
| **WorkBuddy** | 开放平台技能市场搜「会话分叉」；或 `skillhub install session-fork --namespace user_5b43da63`；或在 [SkillHub](https://skillhub.cn) 搜 `session-fork` | `~/.workbuddy/skills/session-fork/` | 直接对 WorkBuddy 说「打分支，命名『…』」 |
| **Claude Code / Codex / Hermes / pi / OpenClaw** | **不用装"技能"**：`pip install git+https://github.com/yamingmou/session-fork-core.git`（或 `git clone`） | 你本机，任意目录 | `fork --adapter <产品> --session current --name "<分支名>"` |
| **只想当命令行工具**（默认操作 WorkBuddy） | 同上 | 你本机 | `fork --session current --name "<分支名>"` ⚠️ **非 WorkBuddy 用户必须补 `--adapter <你的产品>`**，否则命令会落到 WorkBuddy 的会话库 |

> **非 WorkBuddy 用户要点**
> ① 不需要 WorkBuddy，也**不需要把文件放进任何"技能目录"**——它就是一个 Python 命令行工具；
> ② 用 `--adapter` 指定你在用的产品；各产品的会话库位置**自动探测**，也可用环境变量覆盖（如 `CODEX_HOME` / `HERMES_HOME`）；
> ③ 下面命令行示例里的 `fork`，就是 `pip install` 装出来的那个命令。

<details>
<summary>GitHub 源码方式（开发 / 自测用）</summary>

```bash
git clone https://github.com/yamingmou/session-fork-core.git
python3 <clone目录>/scripts/create_branch.py --session current

# 自测
python3 <clone目录>/tests/test_wb_adapter.py
```
</details>

## 使用

### 对 WorkBuddy 说

- `打分支，命名『论文讨论』` → 复制当前这段工作，从上一轮结束处切开（对话、任务、方案都一样）；
- `打分支，从『下一步你可以选』那条回复作为拆分点，命名『方案对比』` → 从你指定的那一条重新走；
- 贴一段"复制请求 ID"（来自**任何会话**）→ 从那个会话的该条回复处打分支；
- 想并行试几条路 → 同一段工作连打两条分支，各命名、各走各的。

### 命令行

> **读法约定**：为简洁，下面用 `fork` 代表命令入口。
> **通过技能市场安装的人没有 `fork` 这个命令**（它是 `pip install` 之后才有的别名）——请把它换成完整路径：
> `python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py`。**两者参数完全一致。**

```bash
# 打当前对话的分支（默认截断到上一轮输出结束）
fork --session current --name "<分支名>"

# 从某条回复打（请求 ID 从产品 UI "复制请求ID" 获取）
fork --session current --request-id "<conversationRequestId>" --name "<分支名>"
fork --request-id "<conversationRequestId>" --name "<分支名>"   # 自动定位所属对话

# 按特征文本截断 / 预览
fork --session current --match "<拆分点特征文本>" --name "<分支名>"
fork --session current --dry-run

# 查看分支 / 谱系树
fork --list
fork --list --tree
```

### 实际输出长这样

截断点、产物 id、校验结论一目了然——**不确认就不落盘**：

```text
Source   : <源会话 id>   (…/projects/<工作区>/<源会话 id>.jsonl)
Split    : line 76 / 76  (default (previous turn's output end))
Branch   : <新会话 id>   name='论文讨论'
Verify   : ✅ OK (6 lines, sessionId consistent, zero residue, tail complete)
DRY RUN — nothing written.        # 仅 --dry-run 时出现；不加则真正落盘
```

## 工作原理

打分支是一次**存储级复制**：取源会话 transcript 的第 `1..截断点` 行 → 递归改写其中的会话 id → 写成新的会话文件 → 在会话索引与谱系索引里登记。源会话零改动。

底层是 **fork-core 通用引擎 + 各产品适配器**（内部实现，不影响使用）：

```
session-fork/
├── SKILL.md                    # 技能定义（给 AI 读的操作说明）
├── scripts/create_branch.py    # 唯一入口 ← 只跑这个
├── fork_core/                  # 引擎 + 各产品适配器
└── tests/                      # 自测（开发用）
```

**支持哪些产品看上面「当前支持」**；`fork_core/` 的内部结构属开发者文档，不影响使用——
**请不要绕过入口直接执行里面的模块**（唯一入口是 `scripts/create_branch.py`）。
新增一个产品支持 = 新增一个 `adapter_<产品>.py`，引擎零改动（旧适配器也不需要改动）。

## 相关链接

- **GitHub 仓库**：https://github.com/yamingmou/session-fork-core
- **SkillHub**：https://skillhub.cn 搜索 `session-fork`（作者 `@user_5b43da63`）
- **WorkBuddy 开放平台**：https://open.workbuddy.cn/ （技能市场搜索 `会话分叉`）

## 署名 / About

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。我们将 WorkBuddy 用于日常开发工作流，这个技能是我们对社区的回馈。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. We use WorkBuddy as part of our daily development workflow; this skill is our way of giving back to the ecosystem. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

## License

MIT — 自由使用、修改与再分发，保留署名即可。

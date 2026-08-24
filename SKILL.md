---
name: session-fork
slug: session-fork
displayName: WorkBuddy 会话分叉（打分支）
description: 把当前（或指定）WorkBuddy 会话复制成一个独立新分支（时间线分叉）——默认截断点 = 上一轮对话的输出结束（无需指定任何文本），也可按用户指定的某条回复/特征文本截断；截取会话前缀生成独立新会话，之后原会话继续、分支独立存在。This skill should be used when the user asks to 打分支 / 会话分叉 / 对话分支 / 复制对话成新分支 / split session / fork session / branch this conversation / 以某条回复为界新建对话 / 把对话截断复制。典型指令："打分支，命名『论文讨论』"（默认截断到上一轮输出结束）或"打分支，从『…』那条回复作为拆分点，命名『…』"。
version: 1.2.0
author: OfferKuai (Offer快) Team
license: MIT
tags: [workbuddy, session, fork, conversation, 会话分叉, 打分支, 办公效率, 会话管理, 对话管理, 效率]
agent_created: true
---

# Session Fork（会话分叉 · 打分支）

把当前（或指定）会话复制为**独立新分支**：默认以上一轮对话的输出结束为截断点（也可按用户指定的回复截断），截取前缀（1..截断点），改写会话 id 后存入存储，原会话不受影响。

一句话价值：**回到某个节点继续讨论，而不丢任何历史**——适合"这个方向聊岔了，我想回到上一轮重新来"、"同一个主题开几条平行线分别讨论"的时间线分叉场景。

## 功能特性

- **零配置默认模式**：用户只说"打分支"即可，截断点自动 = 上一轮对话的输出结束，无需提供任何拆分点文本；
- **精确指定模式**：按用户引用的某条回复特征文本（`--match`）或行号（`--line`）截断；
- **存储级复制**：新文件 + 新数据库行，不是"链接/指向"——原会话后续写入不会污染分支；
- **内置安全**：执行前自动备份（jsonl + 数据库），嵌套字段旧 id 全量替换，自带完整性校验；
- **可预览**：`--dry-run` 先确认截断点定位，再正式执行。

## 触发条件

用户**明确要求创建/执行**"打分支 / 会话分叉 / 复制对话 / 分支会话 / split session / fork session / 新建分支 / 从这里分叉"。两种模式：

- **默认模式（推荐，无需额外信息）**：用户只说"打分支"或"打分支，命名『X』"——**截断点自动 = 上一轮对话的输出结束**（用户发起打分支之前，最后一条完整 assistant 回复的末尾）。这是本技能的默认行为，形成肌肉记忆，不用每次重新思考。
- **指定模式**：用户给出拆分点特征文本（如"从『下一步你可以选』那条回复作为拆分点"）或行号——按指定点截断。

### 排除（不触发执行，只回答问题）

以下场景用户只是**咨询/了解**，不应执行分叉操作：

- "什么是分叉 / 分叉有什么用 / 怎么用分叉功能" → 解释功能，不执行
- "帮我看看当前有没有分支 / 列出分支" → 用 `--list` 查询，不创建
- "这个对话太长了" / "对话需要整理" → 不自动推断要分叉，询问用户意图
- "能不能回到之前的某个点" → 解释可以用分叉实现，询问是否执行
- 用户在讨论分叉的概念/原理/对比 → 只回答，不执行

## 工作流程

### Step 1 — 确认源会话

- 用户说"当前对话"→ 查数据库最新 `status='working'` 的会话（脚本 `--session current` 自动解析）；
- 用户指定会话 → 直接使用该 id。
- 会话存储：`~/.workbuddy/projects/<workspace-slug>/<session-id>.jsonl`（slug = cwd 去 `/` 后 `/` 换 `-`，如 `Users-maxwell-WorkBuddy-2026-08-20-19-07-45`）。
- 会话元数据在 `~/.workbuddy/workbuddy.db` 的 `sessions` 表（字段含 id/custom_title/status/created_at/cwd 等）。

### Step 2 — 确认截断点（默认规则优先，勿跳过）

**默认规则：每个分支的截断点 = 上一轮对话的输出结束。**
- 语义：用户发起"打分支"时，分支应精确收尾在**该指令之前最后一条完整 assistant 回复**的末尾（`output_text` 完整输出结束）——即用户最后一条 user 消息之前的最后一条 assistant 回复；
- 实现：脚本默认模式（不带 `--match`/`--line`）自动定位，无需人工找行号；
- **指定覆盖**：仅当用户明确给出拆分点特征文本时才用 `--match`（取最后一条匹配的 assistant 回复）；给出行号才用 `--line`；
- **边界校验**：截断行的下一行必须是新的 user 消息或 EOF——否则截断点落在未完成的回复中间，分支末条会是残缺消息（默认模式天然满足：截断点紧邻最后一条 user 消息之前）。

### Step 3 — 执行创建（用脚本，勿手写）

```bash
# 默认模式：截断到上一轮对话输出结束，名称自动从主题生成
python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py \
  --session current

# 默认模式 + 自定义名称
python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py \
  --session current --name "<分支名>"

# 指定模式：按特征文本截断
python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py \
  --session current --match "<拆分点特征文本>" --name "<分支名>"

# 查询当前工作区的所有分支
python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py --list

# 修复一个被 WorkBuddy 追加了多余消息的分支
python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py \
  --fix <分支会话ID>
```

脚本自动完成：备份（jsonl + db 到 `~/.workbuddy/backups/<时间戳>/`）→ 定位截断点 → 截取 1..截断点 → 顶层 `sessionId` 改为新 UUID → **嵌套字段旧 id 全量替换** → db `sessions` 插入新行（复制源行，改 id/custom_title/status=terminated/时间戳）→ 验证 → **文件锁为只读（0444）防止 WorkBuddy 追加消息**。

先跑 `--dry-run` 确认截断点定位正确，再正式执行（推荐）。

### Step 4 — 验证与汇报

脚本自带验证（行数/解析/sessionId 一致性/零残留/末条完整）。汇报**必须使用以下固定模板**：

```
✅ 分支创建完成

📋 分支信息
- 分支 ID：<new-session-id>
- 名称：<custom_title>
- 截断位置：第 N 行 / 共 M 行（默认模式注明"上一轮输出结束"）
- 分支行数：N 行

📦 原会话不受影响
- 原会话 ID：<src_id>（继续正常使用）

⚠️ 请重启 WorkBuddy 以在侧边栏看到新分支
- macOS：⌘Q 退出后重新打开，或终端执行 `open -a WorkBuddy`

💡 下一步
- 重启后在侧边栏选择新分支继续讨论
- 或在当前对话继续（分支已独立保存，不会丢失）
```

查询分支时（`--list`）使用：

```
📂 当前工作区的分支列表
- <id> | <名称> | <状态> | <创建时间>
- ...
（共 N 个分支，无分支时提示"当前工作区暂无分支"）
```

### Step 5 — 记录（可选）

团队协作时，建议把分支创建记录追加到项目日志（如 `.workbuddy/memory/YYYY-MM-DD.md`）：新 id、行数、截断点、custom_title、备份路径——便于日后回溯"这个分支从哪条线分出来的"。

## 分支命名建议（可自定义）

- 分支体系用序号区分：`XX·分支A｜描述` / `分支B｜描述` / `分支C｜描述`；
- `custom_title` 建议格式：`<主题>·分支X｜<用途>`，如 `DSH审计·分支C｜论文讨论`；
- status 一律 `terminated`（无活跃 agent 的正常终态，不影响打开查看）。

## 关键坑位（实战踩过）

1. **默认截断点 = 上一轮对话输出结束**：用户说"打分支"时不要反问"从哪条回复截断"——默认截到用户最后一条消息之前最后一条完整 assistant 回复的末尾（脚本默认模式自动定位，dry-run 验证即可）。
2. **嵌套字段旧 id 残留**：只改顶层 `sessionId` 不够——`output.text` / `providerData.toolResult.content` / `arguments` / `reasoning` / `rawContent` 里都会出现旧 id（一次实测 87 处）。必须对整行 JSON 做字符串级 `旧id → 新id` 替换，否则分支会"引用"旧会话的工具结果。
3. **指定模式边界**：用户引用文本可能出现在多条回复里，取最后一条；且必须确认该回复是完整收尾（下一行是 user 消息）。
4. **快照分支特性**：复制发生在读取时刻，原会话之后的新消息不会进分支——这是正常行为，不是丢数据。
5. **附属目录 tool-results/**：是运行时输出缓存，jsonl 已内嵌完整 function_call_result，分支**不需要**复制附属目录（或建空目录即可）。
6. **先备份再动手**：脚本已内置备份到 `~/.workbuddy/backups/<时间戳>/`，不要跳过。

## 边界

- 本技能做的是**存储级复制**（新文件 + 新 db 行），不是"链接/指向"——链接会让原会话后续写入污染分支，且无法表达"截断到某行为止"。
- 分支创建后如需删除，由用户决定，不擅动。
- 仅面向 WorkBuddy 会话存储格式（`~/.workbuddy/projects/*/*.jsonl` + `workbuddy.db`），其他平台不适用。

---

## 署名 / About

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. We use WorkBuddy as part of our daily development workflow; this skill is our way of contributing back to the ecosystem. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。我们将 WorkBuddy 用于日常开发工作流，这个技能是我们对社区的回馈。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

## License

MIT License — free to use, modify and redistribute with attribution.

**Source (GitHub):** https://github.com/yamingmou/workbuddy-session-fork
**Install (SkillHub):** `skillhub install session-fork --namespace user_5b43da63`（或 https://skillhub.cn 搜索 `session-fork`）

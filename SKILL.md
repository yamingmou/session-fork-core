---
name: session-fork
slug: session-fork
displayName: 会话分叉（打分支）
description: 把当前（或指定）AI 会话复制成一个独立新分支（时间线分叉）——默认截断点 = 上一轮对话的输出结束（无需指定任何文本），也可按用户指定的某条回复/特征文本截断；截取会话前缀生成独立新会话，之后原会话继续、分支独立存在。基于 fork-core 通用引擎（Fork = Projection Derivative 投影派生），跨产品可用（WorkBuddy 适配器默认，Claude Code 等适配器验证中）。This skill should be used when the user asks to 打分支 / 会话分叉 / 对话分支 / 复制对话成新分支 / split session / fork session / branch this conversation / 以某条回复为界新建对话 / 把对话截断复制。典型指令："打分支，命名『论文讨论』"（默认截断到上一轮输出结束）或"打分支，从『…』那条回复作为拆分点，命名『…』"。
version: 2.4.0
author: OfferKuai (Offer快) Team
license: MIT
tags: [workbuddy, claude-code, session, fork, conversation, 会话分叉, 打分支, 办公效率, 会话管理, 对话管理, 效率]
agent_created: true
---

<h1><img src="https://raw.githubusercontent.com/yamingmou/session-fork-core/main/logo.png" width="40" height="40" alt="Fork Logo" style="vertical-align: middle;"> Session Fork（会话分叉 · 打分支）</h1>

把当前（或指定）会话复制为**独立新分支**：默认以上一轮对话的输出结束为截断点（也可按用户指定的回复截断），截取前缀（1..截断点），改写会话 id 后存入存储，原会话不受影响。

一句话价值：**回到某个节点继续讨论，而不丢任何历史**——适合"这个方向聊岔了，我想回到上一轮重新来"、"同一个主题开几条平行线分别讨论"的时间线分叉场景。

## 功能特性

- **零配置默认模式**：用户只说"打分支"即可，截断点自动 = 上一轮对话的输出结束，无需提供任何拆分点文本；
- **精确指定模式**：按用户引用的某条回复特征文本（`--match`）、行号（`--line`）或请求ID（`--request-id`）截断；
- **存储级复制**：新 jsonl 文件 + 会话索引新行记录，不是"链接/指向"——原会话后续写入不会污染分支；
- **谱系可追溯**：`parent_id` + `at_seq`（快照点）记录分支从哪派生，`--list --tree` 展示分叉树；
- **快照点可回**：分支可再派生（从分支再 fork = 新投影继续演进）；
- **内置安全**：执行前自动备份（仅源 jsonl），递归 id 替换（rawContent/rawResponse 等原始内容黑名单不碰），自带完整性校验；
- **可预览**：`--dry-run` 先确认截断点定位，再正式执行。

## 内部架构（通用引擎）

脚本基于 **fork-core 通用引擎 + 产品 adapter** 设计（内部实现，不影响使用）：

```
fork_core/                    # 通用引擎（与产品无关）
├── engine.py                 # 截断点定位/截取/备份/验证/汇报
├── models.py                 # SessionMeta / ForkResult 契约
└── adapters/
    ├── base.py               # TranscriptionAdapter 接口
    ├── workbuddy.py          # WorkBuddy 适配器（默认，真库实测）
    └── claude_code.py        # Claude Code 适配器（修改中/验证中：fixture 级验证通过，真实 CLI 会话验证待做，暂不宣传）
```

- 核心逻辑（默认/--match/--line/--request-id 定位、结构化 id 替换、完整性校验）全部在引擎层，与存储格式无关；
- 每个产品只实现一个 adapter（4 组方法：定位/消息判定/读写/注册），格式差异被完全隔离；
- 未来新增产品支持 = 新增一个 adapter 文件，引擎零改动。

## 触发条件

用户**明确要求创建/执行**"打分支 / 会话分叉 / 复制对话 / 分支会话 / split session / fork session / 新建分支 / 从这里分叉"。

**用户的唯一心智（技能只认这一条，无需用户理解内部概念）：**
> **没贴 conversation ID → 打当前对话的分支；贴了 conversation ID（"复制请求 ID"的 JSON）→ 打那个对话的分支。**

- **默认**：用户只说"打分支"或"打分支，命名『X』"（没贴任何 ID）——源会话 = 当前对话，截断点 = 上一轮对话输出结束。
- **从某条回复打**：用户贴了复制的 ID（可能来自本对话或任何其他对话），或说了"从『XXX』那条回复打"——按用户给的信息定位源会话和断点。
- **给 AI 的执行映射**（AI 判断用哪个命令，不把选择抛给用户）：

| 用户给了什么 | AI 用什么命令 |
|---|---|
| 什么都没贴（当前对话） | `fork --session current`（默认截断点） |
| @引用了一段内容（long-text quote）说打分支 | 从引用 JSON 提取 id（格式 `<sessionId>-<requestId>`，如 `"ec48e1ae-…-e683a22a…"`）→ `fork --session <sessionId> --request-id <requestId>` |
| 贴完整 JSON（conversationId + conversationRequestId） | `fork --session <conversationId> --request-id <conversationRequestId>`（conversationId = 会话 ID，直接定位） |
| 只贴了 conversationRequestId / traceId | `fork --request-id <id>`（自动反查该 ID 所属会话，跨工作区） |
| 说了文本/行号 | `fork --session current --match "XXX"` 或 `--line N` |

> **⚠️ 铁律（2026-09-03 实战事故固化）**：用户给了**任何形式的引用**（@long-text 引用 / 复制的请求 ID JSON / 纯 requestId / 指向别处的会话内容）→ **源会话 = 引用所指的那个会话，严禁默认 `--session current` 打当前对话**。识别引用 ID：JSON 里找 `conversationId`，或 @引用内容里找 `"id": "<sessionId>-<requestId>"` 双段拼接格式。拿不准时先 `--dry-run` 展示将要打源会话名 + 断点，问用户确认再正式执行——**绝不反复试错创建分支**。

### 排除（不触发执行，只回答问题）

以下场景用户只是**咨询/了解**，不应执行分叉操作：

- "什么是分叉 / 分叉有什么用 / 怎么用分叉功能" → 解释功能，不执行
- "帮我看看当前有没有分支 / 列出分支" → 用 `--list` 查询，不创建
- "这个对话太长了" / "对话需要整理" → 不自动推断要分叉，询问用户意图
- "能不能回到之前的某个点" → 解释可以用分叉实现，询问是否执行
- 用户在讨论分叉的概念/原理/对比 → 只回答，不执行

## 工作流程

### Step 1 — 确认源会话

按上表映射，先看用户贴了什么：
- 什么都没贴 → `--session current`（数据库最新 `status='working'` 的会话 = 当前对话）；
- 贴了**完整 JSON**（UI 复制请求 ID 的原始格式）→ 取 `conversationId` 作为源会话（**conversationId 就是会话文件名**，直接定位，不扫描）；`conversationRequestId` 作为断点；
- 贴了**纯 requestId/traceId**（无 conversationId）→ 用 `--request-id` 自动反查该 ID 属于哪个会话（跨工作区兜底，9b054ee 起按 mtime 新→旧搜，提速 16x）。
- 会话存储：`~/.workbuddy/projects/<workspace-slug>/<session-id>.jsonl`（slug = cwd 去 `/` 后 `/` 换 `-`）。
- 会话元数据在 `~/.workbuddy/workbuddy.db` 的 `sessions` 表。

### Step 2 — 确认截断点（默认规则优先，勿跳过）

**⚠️ 最高优先级规则：用户说了断点 → 必须用 `--match` 或 `--line`，绝对不能用默认模式！**

默认模式只在用户**完全没提断点**（只说"打分支"）时才用。一旦用户描述了任何断点信息（"截断到 XXX"、"从 XXX 之前分叉"、"到 XXX 产生处"），就必须用指定模式。

**三种场景的处理方式：**

| 用户说的 | 用什么 | 怎么做 |
|---|---|---|
| "打分支"（没提断点） | 默认模式 | 脚本自动定位最后一条 assistant 回复 |
| "截断到这条回复" + 提供了请求ID | `--request-id` | **最精确**，直接匹配 conversationRequestId |
| "截断到 XXX" | `--match "XXX"` | 取最后一条包含 XXX 的 assistant 回复 |
| "截断到 XXX 产生处" | `--line N` | 先用 grep/Read 在源会话 jsonl 中搜索 XXX 所在行号，再用 `--line` |

**⚠️ 获取请求ID的方法（最精确的截断点）：**
1. 在 WorkBuddy UI 中，点击目标断点处的**助手回复**；
2. 点击"**复制请求ID**"按钮，得到 JSON 如 `{"traceId":"...","conversationRequestId":"abc123","conversationId":"..."}`;
3. 用 `--request-id abc123` 截断——**唯一标识一条回复，不会误匹配**。

**⚠️ 关键：当用户口头描述断点但没给精确文本时（如"截断到项目思维规则模板产生处"），必须先在源会话 jsonl 中搜索确认行号，然后用 `--line`，绝不能忽略断点用默认模式。**

**断点定位步骤（指定模式）：**
1. 在源会话 jsonl 中搜索用户描述的关键词（`grep -n "关键词" <session-id>.jsonl`）；
2. 确认匹配行是 assistant 消息且有 output_text（必须是完整回复，下一行是 user 消息或 EOF）；
3. 用 `--line N` 或 `--match "精确文本"` 截断；
4. 跑 `--dry-run` 验证定位正确后，再正式执行。

**默认模式规则（仅当用户完全没提断点时）：**
- 截断点 = 上一轮对话的输出结束（用户最后一条 user 消息之前的最后一条完整 assistant 回复）；
- 脚本自动定位，`--dry-run` 验证即可。

### Step 3 — 执行创建（用脚本，勿手写）

**执行方式（三选一，跨平台）**：
1. **pip 安装（推荐，跨平台）**：`pip install fork-core` → 全局 `fork` 命令（Windows/macOS/Linux 通用）；
2. **WorkBuddy 技能目录**：`python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py`（SkillHub 安装后自带）；
3. **git clone 任意目录**：`python3 <任意目录>/scripts/create_branch.py`（脚本自动定位自身，不依赖 cwd）。

```bash
# 场景 1：当前对话打分支（用户没贴任何 ID）
fork --session current --name "<分支名>"                 # 默认截断到上一轮输出结束
fork --session current --match "<拆分点特征文本>"          # 从当前对话某条回复打

# 场景 2：从贴的 conversation ID 打（用户贴了"复制请求 ID"，可能来自任何对话/工作区）
fork --session "<conversationId>" --request-id "<conversationRequestId>" --name "<分支名>"
#   ↑ conversationId 即 UI 复制 JSON 里的 conversationId（= 源会话 ID），直接定位
# 只拿到 requestId/traceId 时：自动反查所属会话（无需 --session）
fork --request-id "<conversationRequestId>" --name "<分支名>"

# 运维类
fork --list                                        # 查询分支
fork --fix <分支会话ID>                              # 修复被追加消息的分支（仅 workbuddy）
fork --verify                                      # 真库体检（发布前必跑），别名 --doctor
fork --session current --adapter claude-code       # 其他产品（验证中，不宣传）
```

**执行规范**：
- **先 `--dry-run` 确认截断点，再正式执行**（推荐，防打错位置）；
- **`fork --verify` 真库体检**：发布/环境变化后必跑；打分支前建议跑——环境异常会 FAIL 拦截（Claude adapter 无真实 CLI 会话时属预期 L1）；
- 脚本自动完成备份 → 截取 → 会话 id 替换 → 注册 → 校验，无需手工介入。

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

1. **用户说了断点 → 必须用 --match/--line，绝对不能用默认模式**：默认模式找的是"最后一条 assistant 回复"（文件末尾），不是用户指定的中间位置。曾因用默认模式执行"截断到 XXX 产生处"的指令，导致分支包含了不该有的后续对话（多出 13-22 行），需要事后用 --fix 修复。
2. **默认截断点 = 上一轮对话输出结束**：用户只说"打分支"没指定断点时，默认截到用户最后一条消息之前最后一条完整 assistant 回复的末尾（脚本默认模式自动定位，dry-run 验证即可）。
3. **嵌套字段旧 id 残留**：只改顶层 `sessionId` 不够——`output.text` / `arguments` / `argumentsDisplayText` / `toolResult.renderer.value` / `error.message` 等字段都会出现旧 id。v2.4.0 起使用**递归 id 替换**（覆盖全部可读字段，仅 rawContent/rawResponse 等原始内容黑名单不碰），WorkBuddy 与 Claude 两个 adapter 机制一致。实测一次打分支替换 2557 处。
4. **指定模式边界**：用户引用文本可能出现在多条回复里，取最后一条；且必须确认该回复是完整收尾（下一行是 user 消息）。
5. **WorkBuddy 会追加消息到分支文件**：脚本创建分支后，WorkBuddy 主进程可能仍向该 jsonl 追加新消息。v1.4.0 起不再自动锁只读（执行后提示用户手动 `chmod 444`），已有分支可用 --fix 修复。
6. **快照分支特性**：复制发生在读取时刻，原会话之后的新消息不会进分支——这是正常行为，不是丢数据。
7. **附属目录 tool-results/**：是运行时输出缓存，jsonl 已内嵌完整 function_call_result，分支**不需要**复制附属目录（或建空目录即可）。
8. **先备份再动手**：脚本已内置备份到 `~/.workbuddy/backups/<时间戳>/`，不要跳过。

## 边界

- 本技能做的是**存储级复制**（新 jsonl 文件 + 会话索引新行记录），不是"链接/指向"——链接会让原会话后续写入污染分支，且无法表达"截断到某行为止"。
- 分支创建后如需删除，由用户决定，不擅动。
- 适配边界：只适配「会话 transcript 本地落盘、格式开放/稳定」的开发者工具（WorkBuddy 默认，Claude Code 验证中；C 端云优先 SaaS 如元宝/千问因本地无 transcript 不在范围）。

---

## 署名 / About

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. We use WorkBuddy as part of our daily development workflow; this skill is our way of contributing back to the ecosystem. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。我们将 WorkBuddy 用于日常开发工作流，这个技能是我们对社区的回馈。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

## License

MIT License — free to use, modify and redistribute with attribution.

**Source (GitHub):** https://github.com/yamingmou/session-fork-core
**Install (SkillHub):** `skillhub install session-fork --namespace user_5b43da63`（或 https://skillhub.cn 搜索 `session-fork`）

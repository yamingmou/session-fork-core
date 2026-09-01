<h1><img src="https://raw.githubusercontent.com/yamingmou/session-fork-core/main/logo.png" width="40" height="40" alt="Fork Logo" style="vertical-align: middle;"> Session Fork（会话分叉 · 打分支）</h1>

> **Fork = Projection Derivative（投影派生）**：任何「会话/对话/任务」是一个投影；分支 = 从某个**快照点**派生新投影——继承截断点之前的全部历史（记忆不丢），新投影独立演进，原投影不变（生产基线），谱系可追溯。

把一段对话**复制**出一个独立分支：默认以上一轮对话的输出结束为截断点（也可按你指定的某条回复截断），截取会话前缀生成**新会话**，之后原会话继续、分支独立发展。适合"这个方向聊岔了，想回到上一轮重新来"、"同一主题开几条平行线分别讨论"的场景。

- 名称：`session-fork`（会话分叉 / 打分支）｜引擎：`fork-core`（跨产品通用引擎）
- 适用平台：WorkBuddy（默认适配器）· Claude Code（验证中）· Codex / opencode（规划中）
- 版本：2.3.0
- 作者：OfferKuai（Offer快）团队
- 许可证：MIT

## 功能特性

- **零配置默认模式**：只说"打分支"即可，截断点自动 = 上一轮对话的输出结束；
- **精确指定模式**：按回复特征文本（`--match`）、行号（`--line`）或请求ID（`--request-id`）截断；
- **存储级复制**：新 jsonl 文件 + sessions 表新行记录，原会话后续写入不会污染分支；
- **谱系可追溯**：`parent_id` + `at_seq`（快照点）记录每个分支从哪派生，`--list --tree` 展示分叉树；
- **快照点可回**：分支可再派生（从分支再 fork = 新投影继续演进，谱系树延伸）；
- **内置安全**：执行前自动备份（仅源 jsonl），递归 id 替换（rawContent/rawResponse 等原始内容黑名单不碰），自带完整性校验；
- **可预览**：`--dry-run` 先确认截断点，再正式执行。

## 安装

### 方式一：pip 安装（推荐，跨平台）

Windows / macOS / Linux 统一：

```bash
# PyPI 发布后：
pip install fork-core

# 当前（发布前）：从 GitHub 安装
pip install git+https://github.com/yamingmou/session-fork-core.git

# 安装后获得全局命令
fork --version
```

### 方式二：SkillHub（WorkBuddy 用户）

```bash
skillhub install session-fork --namespace user_5b43da63
```

或在 [SkillHub 官网](https://skillhub.cn) 搜索 `session-fork` 安装。

> **渠道版本**：SkillHub 与 GitHub 均已发布 v2.x（fork-core 引擎版）。Claude Code 适配器为**验证中**（fixture 级通过，真实 CLI 会话验证待做，暂不宣传）。

### 方式三：GitHub 源码（任意目录）

```bash
git clone https://github.com/yamingmou/session-fork-core.git

# 脚本自动定位自身，任意 cwd 可运行：
python3 <clone目录>/scripts/create_branch.py --session current
```

### 各平台会话数据位置（adapter 自动识别，无需配置）

| 产品 | macOS / Linux | Windows |
|---|---|---|
| WorkBuddy | `~/.workbuddy/projects/` | `%USERPROFILE%\.workbuddy\projects\` |
| Claude Code | `~/.claude/projects/` | `%USERPROFILE%\.claude\projects\` |

## 相关链接

- **GitHub 仓库**：https://github.com/yamingmou/session-fork-core
- **SkillHub 技能**：https://skillhub.cn （搜索 `session-fork`，作者 `@user_5b43da63`）

## 使用

对 WorkBuddy 说：

- `打分支，命名『论文讨论』` → 以上一轮对话输出结束为界，复制出一个名为「论文讨论」的分支；
- `打分支，从『下一步你可以选』那条回复作为拆分点，命名『方案对比』` → 按指定回复截断；
- `打分支` → 纯默认，截到上一轮对话输出结束。

### 命令行

> `fork` 命令 = pip 安装的全局命令（跨平台）；WorkBuddy 技能目录用户也可用
> `python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py`（等价）。

```bash
# 默认模式：截断到上一轮对话输出结束
fork --session current --name "<分支名>"

# 最精确：按请求ID截断（从产品 UI "复制请求ID" 获取）
fork --session current --request-id "<conversationRequestId>" --name "<分支名>"

# 指定模式：按特征文本截断
fork --session current --match "<拆分点特征文本>" --name "<分支名>"

# 查询当前工作区的所有分支
fork --list

# 查看分叉谱系树（父→子→孙）
fork --list --tree

# 修复被主进程追加了多余消息的分支（仅 workbuddy）
fork --fix <分支会话ID>

# 先预览截断点，不写入
fork --session current --dry-run

# 真库体检（发布/打分支前必跑）：存储/schema/真实数据替换/谱系 全项检查
fork --verify            # 别名 --doctor；任何一项 FAIL 则 exit 1 拦截
```

> **真库验证是必须项**：`fork --verify` 做 L2 真库体检——数据库存在性 + sessions 表关键
> NOT NULL 约束 + 真实会话数据上递归 id 替换零残留 + 谱系索引可读。发布前必跑；
> 任一 FAIL 会以 exit 1 拦截（防止"fixture 通过 ≠ 真库验证"的问题漏到线上）。

## 工作原理

1. 定位会话 transcript（`~/.workbuddy/projects/<workspace-slug>/<session-id>.jsonl`）；
2. 定位截断点：默认 = 最后一条完整 assistant 回复的末尾；`--request-id`/`--match`/`--line` 可精确指定；
3. 备份源 jsonl 到 `~/.workbuddy/backups/<时间戳>/`（不复制数据库）；
4. 截取前缀（1..截断点），顶层 `sessionId` 改为新 UUID，**递归 id 替换**（覆盖 output/arguments/renderer/error 等全部可读字段，rawContent/rawResponse 等原始内容黑名单不碰）；
5. sessions 表插入新行（复制源行，status=terminated）；
6. **谱系记录**：`~/.workbuddy/fork.lineage.json` 旁路索引写入 parent_id + at_seq（快照点），不污染官方 schema；
7. 校验：可解析性、sessionId 一致性、零旧 id 残留、末条完整性。

## 内部架构（fork-core 通用引擎）

脚本基于 **fork-core 通用引擎 + 产品 adapter** 设计：

```
fork_core/                    # 通用引擎（与产品无关）
├── engine.py                 # 截断点定位/截取/备份/验证/谱系
├── models.py                 # SessionMeta（含 parent_id）/ ForkResult 契约
└── adapters/
    ├── base.py               # TranscriptionAdapter 接口
    ├── workbuddy.py          # WorkBuddy 适配器（默认，真库实测）
    └── claude_code.py        # Claude Code 适配器（修改中/验证中：fixture 级通过，真实 CLI 会话验证待做，暂不宣传）
```

- 核心逻辑（截断定位、结构化 id 替换、完整性校验、谱系）全部在引擎层，与存储格式无关；
- 每个产品只实现一个 adapter，格式差异被完全隔离——**新增产品 = 新增一个 adapter 文件，引擎零改动**。

## 路线图（Roadmap）

> 本项目是 **Agent 业务层抽象**的会话级实现——把「分叉」从单一产品的功能，抽象成
> 跨产品、可被上层消费的底层能力。

**已实现（会话级分支）**
- ✅ 存储级复制 + 4 种截断定位（默认/--match/--line/--request-id）
- ✅ 递归 id 替换（rawContent 黑名单）+ 完整性校验 + 自动备份
- ✅ 谱系可追溯（parent_id + at_seq 快照点）+ 分叉树展示
- ✅ 快照点可回（分支可再派生）
- ✅ 跨平台安装（pip / SkillHub / git clone）
- ✅ WorkBuddy adapter 正式；Claude Code adapter 验证通过

**开发中 / 规划中**
- 🚧 **跨产品适配器矩阵**：Codex（rollout 文件，含 `forked_from_id` 语义）、opencode（SQLite + 版本化迁移）——引擎已支持，adapter 待落地
- 🚧 **业务层接口固化**：`createFork(projection, {atSeq})` / `listForks(projection)` 固化为框架无关契约，被上层（dsh-retrace 等生产级运营层）以库的方式调用
- 🔭 **会话内分支（不在此引擎范围）**：同一会话内的版本时间线/分叉图/回退，由上层运营层各自抽象——本引擎只做「会话级派生」，两者正交互补

**分层边界**：fork-core 只管「派生得正确」（新会话怎么诞生）；「派生得健康」（诊断/修复/审计/版本化）是上层（如 dsh-retrace）的职责。产品接入判定三问：① transcript 是否本地落盘？② 格式是否开放/稳定？③ 是否有官方扩展点？

## 边界与已知坑位

- 做的是**存储级复制**（新 jsonl 文件 + sessions 表新行记录），不是"链接/指向"——链接会让原会话后续写入污染分支；
- 快照特性：复制发生在读取时刻，之后原会话的新消息不会进分支（正常行为）；
- 附属目录 `tool-results/` 是运行时缓存，jsonl 已内嵌完整结果，分支无需复制；
- 文件权限保持 0644，不自动锁只读——如需防止 WorkBuddy 追加消息，可手动 `chmod 444 <分支文件>`；
- 适配边界：只适配「会话 transcript 本地落盘、格式开放/稳定」的开发者工具（判定三问见路线图）。

## 署名 / About

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. We use WorkBuddy as part of our daily development workflow; this skill is our way of contributing back to the ecosystem. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。我们将 WorkBuddy 用于日常开发工作流，这个技能是我们对社区的回馈。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

## License

MIT — 自由使用、修改与再分发，保留署名即可。详见 [LICENSE](LICENSE)。

<h1><img src="https://raw.githubusercontent.com/yamingmou/session-fork-core/main/logo.png" width="40" height="40" alt="Fork Logo" style="vertical-align: middle;"> Session Fork（会话分叉 · 打分支）</h1>

把一段对话**复制**出一个独立分支：默认以上一轮对话的输出结束为截断点（也可按你指定的某条回复截断），截取会话前缀生成**新会话**。之后原会话继续，分支独立发展——**回到某个节点继续讨论，而不丢任何历史**。适合"聊岔了想回到上一轮重新来"、"同一主题开几条平行线分别讨论"。

- 当前支持：**WorkBuddy**（打分支技能）；跨平台版本开发中
- 版本：2.4.2
- 作者：OfferKuai（Offer快）团队 | License：MIT

## 功能特性

- **零配置**：说一句"打分支"即可，自动以上一轮对话输出结束为界；
- **精确断点**：可按某条回复（特征文本/请求ID）或行号指定截断点；
- **原会话不受影响**：分支是独立复制，原对话后续内容不会污染分支；
- **谱系可追溯**：每个分支记录来源，`--list --tree` 可视化分叉树；
- **分支可再派生**：从分支还能再打分支；
- **安全**：执行前自动备份，可先预览截断点（`--dry-run`）再执行。

## 安装

### WorkBuddy 用户（推荐）

```bash
skillhub install session-fork --namespace user_5b43da63
```

或在 [SkillHub 官网](https://skillhub.cn) 搜索 `session-fork` 安装。

### 命令行（pip，跨平台）

```bash
pip install git+https://github.com/yamingmou/session-fork-core.git
fork --version
```

### GitHub 源码（任意目录）

```bash
git clone https://github.com/yamingmou/session-fork-core.git
python3 <clone目录>/scripts/create_branch.py --session current
```

## 使用

### 对 WorkBuddy 说

- `打分支，命名『论文讨论』` → 复制当前对话，分支名「论文讨论」；
- `打分支，从『下一步你可以选』那条回复作为拆分点，命名『方案对比』` → 按指定回复截断；
- 贴一段"复制请求 ID"（来自**任何对话**）→ 从那个对话的该回复处打分支。

### 命令行

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

## 相关链接

- **GitHub 仓库**：https://github.com/yamingmou/session-fork-core
- **SkillHub**：https://skillhub.cn 搜索 `session-fork`（作者 `@user_5b43da63`）

## 署名 / About

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

## License

MIT — 自由使用、修改与再分发，保留署名即可。

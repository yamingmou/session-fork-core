<h1><img src="https://raw.githubusercontent.com/yamingmou/session-fork-core/main/logo.png" width="40" height="40" alt="Fork Logo" style="vertical-align: middle;"> Session Fork</h1>

English | [简体中文](README.md)

[![Version](https://img.shields.io/github/v/release/yamingmou/session-fork-core?label=version)](https://github.com/yamingmou/session-fork-core/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-WorkBuddy-blue.svg)](https://open.workbuddy.cn/)
[![Python](https://img.shields.io/badge/python-3.9%2B-informational.svg)](https://www.python.org/)

**Halfway through and want to change direction — without redoing everything?**

Session Fork copies the whole **work-in-progress** into an independent branch: the context, the conclusions you already settled, the steps already taken, the results your tools produced. Resume from any point; the original line keeps running, untouched.

**No re-explaining the background. No re-running the earlier steps. No risk of derailing the original line.**

What gets forked was never just a conversation: **tasks, plans, code, writing, research** — anything that is "half-done, and carries its context and settled conclusions" can be forked.

- *"This direction went wrong — go back to the previous round and start over."* → Branch there and re-walk it.
- *"Same task, I want to try two or three approaches in parallel."* → Open parallel lines and pick one at the end.
- *"The first half is settled — don't touch it."* → A branch is an independent copy (not a link) — whatever happens to the original afterwards never leaks into it.

- Works with: **WorkBuddy** (branching skill). Cross-platform adapters in progress.
- Version: 2.4.7 · By the OfferKuai Team · License: MIT

## What gets forked — and what doesn't

A fork copies **the session's context**. It does not roll the world back. Read this one boundary before you rely on it.

| Artifact | Can the branch return to that moment? |
|---|---|
| In a git repo (already committed) | ✅ Yes. Point the workspace at the matching commit (`git switch -c <name> <sha>` / `git worktree add <dir> <sha>`) and context + artifacts line up again. |
| Recorded elsewhere (cloud-doc revision history, system snapshots / Time Machine, backups, logs) | ✅ Usually. Restore manually to the matching point in time. |
| A plain local file with no version record | ❌ No. Only its **final state** exists. The branch hands you the *old context* next to the *new file*. |
| Side effects that already happened (sent messages / emails, published pages, pushed commits, API writes, installed packages) | ❌ Not undoable. A fork cannot cancel what has already occurred. |

Two clarifications worth knowing:

- What travels with the branch is the **record** of your tool results — the text stored in the transcript — **not** a re-execution of those tools.
- The branch is a **snapshot**: messages added to the original after the fork point will not appear in the branch. That is by design, not data loss.

**Rule of thumb:** if you want a branch to genuinely go back, checkpoint your artifacts first — `git commit` (or `stash` / `tag` / `worktree`). If they have no version record at all and you may need the old version, back it up before you fork.

## Features

- **Zero configuration** — say "fork this" and you're done. The default cut point is the end of the previous turn's output; you never hunt for a breakpoint;
- **You pick the breakpoint** — fork from any specific reply (by distinctive text / request ID / line number);
- **The original stays clean** — a branch is a real, independent copy (not a link), so whatever you do to the original afterwards never leaks into it;
- **Visible lineage** — every branch records where it came from; `--list --tree` shows the whole fork tree at a glance;
- **Keep forking** — a branch can be forked again, and again;
- **Backup and preview first** — the source session is backed up automatically, and `--dry-run` lets you confirm the cut point before anything happens.

## Install

### WorkBuddy users (recommended)

Search for **Session Fork** in the WorkBuddy Open Platform skill marketplace (https://open.workbuddy.cn/), or:

```bash
skillhub install session-fork --namespace user_5b43da63
```

You can also search `session-fork` on [SkillHub](https://skillhub.cn).

### Command line (pip, cross-platform)

```bash
pip install git+https://github.com/yamingmou/session-fork-core.git
fork --version
```

### From source (any directory)

```bash
git clone https://github.com/yamingmou/session-fork-core.git
python3 <clone-dir>/scripts/create_branch.py --session current

# Self-test (development)
python3 <clone-dir>/tests/test_wb_adapter.py
```

## Usage

### Just ask WorkBuddy

- `Fork this, call it "paper discussion"` → copies the current work-in-progress, cut at the end of the previous turn (conversations, tasks and plans alike);
- `Fork from the reply "you can pick next" and call it "option comparison"` → re-walk from that exact reply;
- Paste a copied request ID (from **any** session) → fork that session at that reply;
- Try several approaches in parallel → fork the same work twice, name them, and let each run its own way.

### Command line

```bash
# Fork the current session (cut at the end of the previous turn by default)
fork --session current --name "<branch-name>"

# Fork from a specific reply (get the request ID via "Copy request ID" in the UI)
fork --session current --request-id "<conversationRequestId>" --name "<branch-name>"
fork --request-id "<conversationRequestId>" --name "<branch-name>"   # auto-locates the session

# Cut at a distinctive text / preview only
fork --session current --match "<cut-point text>" --name "<branch-name>"
fork --session current --dry-run

# List branches / show the fork tree
fork --list
fork --list --tree
```

## How it works

A fork is a **storage-level copy**: take lines `1..cut` of the source transcript, rewrite the session ids inside them recursively, write the result to a new session file, and register it in the session index and the lineage index. The source session is not modified at all.

Under the hood it is the **fork-core engine + per-product adapters** (internal detail — it doesn't change how you use it):

```
session-fork/
├── SKILL.md                    # skill definition
├── scripts/create_branch.py    # the only entry point
├── fork_core/                  # product-agnostic engine
│   ├── engine.py               # cut-point location / slicing / backup / verify
│   ├── models.py               # SessionMeta / ForkResult contracts
│   ├── cli.py                  # CLI parsing and output
│   ├── adapters.py             # adapter registry (factory)
│   ├── adapter_base.py         # TranscriptionAdapter interface
│   ├── adapter_workbuddy.py    # WorkBuddy adapter (default, tested on real data)
│   └── adapter_claude_code.py  # Claude Code adapter (in progress)
└── tests/                      # self-tests (development)
```

Adding support for another product means adding one `adapter_<product>.py`; the engine stays untouched.

## Links

- **GitHub**: https://github.com/yamingmou/session-fork-core
- **SkillHub**: https://skillhub.cn — search `session-fork` (author `@user_5b43da63`)
- **WorkBuddy Open Platform**: https://open.workbuddy.cn/ — search `Session Fork` in the skill marketplace

## About

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. We use WorkBuddy as part of our daily development workflow; this skill is our way of giving back to the ecosystem. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。我们将 WorkBuddy 用于日常开发工作流，这个技能是我们对社区的回馈。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

## License

MIT — free to use, modify and redistribute, with attribution.

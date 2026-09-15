<h1><img src="https://raw.githubusercontent.com/yamingmou/session-fork-core/main/logo.png" width="40" height="40" alt="Fork Logo" style="vertical-align: middle;"> Session Fork</h1>

English | [简体中文](README.md)

[![Version](https://img.shields.io/github/v/release/yamingmou/session-fork-core?label=version)](https://github.com/yamingmou/session-fork-core/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Platforms](https://img.shields.io/badge/platforms-WorkBuddy%20%7C%20Claude%20Code%20%7C%20Codex%20%7C%20Hermes%20%7C%20pi%20%7C%20OpenClaw-blue.svg)
[![Python](https://img.shields.io/badge/python-3.9%2B-informational.svg)](https://www.python.org/)

**Halfway through and want to change direction — without redoing everything?**

Session Fork copies the whole **work-in-progress** into an independent branch: the context, the conclusions you already settled, the steps already taken, the results your tools produced. Resume from any point; the original line keeps running, untouched.

**No re-explaining the background. No re-running the earlier steps. No risk of derailing the original line.**

What gets forked was never just a conversation: **tasks, plans, code, writing, research** — anything that is "half-done, and carries its context and settled conclusions" can be forked.

- *"This direction went wrong — go back to the previous round and start over."* → Branch there and re-walk it.
- *"Same task, I want to try two or three approaches in parallel."* → Open parallel lines and pick one at the end.
- *"The first half is settled — don't touch it."* → A branch is an independent copy (not a link) — whatever happens to the original afterwards never leaks into it.

- Works with (**6 products, all verified on a real machine**): **WorkBuddy** (default, verified on a real library) · **Claude Code** · **Codex** · **Hermes** · **pi** · **OpenClaw** (both generations)
- OpenClaw is covered in both generations: `<=2026.6.x` (JSONL) and `>=2026.9.x` (the SQLite `transcript_events` table, isomorphic to JSONL). In both cases the engine's branch is listed by OpenClaw itself and can be continued
- Out of scope: cloud-first services with no local transcript
- By the OfferKuai Team · License: MIT
- Version: **see the Release badge at the top** (not written here — a hand-written version always drifts from the tag)
- **Changelog**: see [CHANGELOG.md](CHANGELOG.md) — every release is written in four parts (value / what's new / changes / verification), and "verification" tells you **how to check it yourself**

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

**First find the product you use** (six are listed above), then take the matching row. **All three entry points take identical parameters — with one exception: if you are not on WorkBuddy, you must add `--adapter <your product>`.**

| You use | How to install | Where it lands | How to run |
|---|---|---|---|
| **WorkBuddy** | Search "Session Fork" in the WorkBuddy Open Platform marketplace; or `skillhub install session-fork --namespace user_5b43da63`; or search `session-fork` on [SkillHub](https://skillhub.cn) | `~/.workbuddy/skills/session-fork/` | Just tell WorkBuddy "打分支 / fork this" |
| **Claude Code / Codex / Hermes / pi / OpenClaw** | **No "skill install" needed**: `pip install git+https://github.com/yamingmou/session-fork-core.git` (or `git clone`) | Your machine, any directory | `fork --adapter <product> --session current --name "<name>"` |
| **CLI only** (operates on WorkBuddy by default) | Same as above | Your machine | `fork --session current --name "<name>"` ⚠️ **If you are not on WorkBuddy, always add `--adapter <your product>`** — otherwise the command hits WorkBuddy's session store |

> **If you are not on WorkBuddy**
> 1. You don't need WorkBuddy, and you **don't need to place files in any "skill directory"** — it is a plain Python CLI.
> 2. Pick your product with `--adapter`; each product's session store is **auto-detected**, and can be overridden by environment variables (e.g. `CODEX_HOME`, `HERMES_HOME`).
> 3. The `fork` used in the examples below is exactly the command `pip install` gives you.

<details>
<summary>From GitHub source (development / self-tests)</summary>

```bash
git clone https://github.com/yamingmou/session-fork-core.git
python3 <clone-dir>/scripts/create_branch.py --session current

# Self-test (development)
python3 <clone-dir>/tests/test_wb_adapter.py
```
</details>

## Usage

### Just ask WorkBuddy

- `Fork this, call it "paper discussion"` → copies the current work-in-progress, cut at the end of the previous turn (conversations, tasks and plans alike);
- `Fork from the reply "you can pick next" and call it "option comparison"` → re-walk from that exact reply;
- Paste a copied request ID (from **any** session) → fork that session at that reply;
- Try several approaches in parallel → fork the same work twice, name them, and let each run its own way.

### Command line

> **How to read these examples**: `fork` below is shorthand for the entry point.
> **If you installed from a skill marketplace you do NOT have a `fork` command** (it only exists after `pip install`) —
> substitute the full path: `python3 ~/.workbuddy/skills/session-fork/scripts/create_branch.py`. Parameters are identical.

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

### What the output looks like

Cut point, new id, and the verification verdict at a glance — **nothing is written until it checks out**:

```text
Source   : <source session id>   (…/projects/<workspace>/<source session id>.jsonl)
Split    : line 76 / 76  (default (previous turn's output end))
Branch   : <new session id>   name='paper-discussion'
Verify   : ✅ OK (6 lines, sessionId consistent, zero residue, tail complete)
DRY RUN — nothing written.        # only with --dry-run; without it the branch is really created
```

## How it works

A fork is a **storage-level copy**: take lines `1..cut` of the source transcript, rewrite the session ids inside them recursively, write the result to a new session file, and register it in the session index and the lineage index. The source session is not modified at all.

Under the hood it is the **fork-core engine + per-product adapters** (internal detail — it doesn't change how you use it):

```
session-fork/
├── SKILL.md                    # skill definition (the doc the AI reads)
├── scripts/create_branch.py    # the only entry point ← run this one
├── fork_core/                  # engine + per-product adapters
└── tests/                      # self-tests (development)
```

**Supported products are listed under "Works with" above.** The internals of `fork_core/` are developer
documentation and do not affect usage — **please don't bypass the entry point by running modules inside it**
(the only entry point is `scripts/create_branch.py`).
Adding support for another product means adding one `adapter_<product>.py`; the engine stays untouched (existing adapters need no changes either).

## Links

- **GitHub**: https://github.com/yamingmou/session-fork-core
- **SkillHub**: https://skillhub.cn — search `session-fork` (author `@user_5b43da63`)
- **WorkBuddy Open Platform**: https://open.workbuddy.cn/ — search `Session Fork` in the skill marketplace

## About

**EN:** Built by the **OfferKuai (Offer快) Team** — an AI startup building full-lifecycle job-application services, guided by the belief that *"users need results, not repeated conversations."* Founder: **Zhaofeng (Yaming)**. We use WorkBuddy as part of our daily development workflow; this skill is our way of giving back to the ecosystem. Website: https://www.offerkuai.com/ | Contact: contact@offerkuai.com

**ZH:** 本技能由 **OfferKuai（Offer快）团队**开发 —— 一家专注 AI 全流程求职托管的创业团队，核心理念是「用户要的是结果，不是重复对话」。创始人：**Zhaofeng（Yaming）**。我们将 WorkBuddy 用于日常开发工作流，这个技能是我们对社区的回馈。官网：https://www.offerkuai.com/ | 联系：contact@offerkuai.com

## License

MIT — free to use, modify and redistribute, with attribution.

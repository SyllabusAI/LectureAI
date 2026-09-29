# Sessions: how to split the work

Companion to `HOME-STRETCH.md`. Both files are local only: the repo ignores
`*.md` except the README, so neither is committed.

## The rule

**One session, one branch, one PR.** A phase is 1 to 5 PRs. A session that
outlives its PR starts reasoning against a repo state it no longer knows,
which is how you get confident and wrong.

**Never `git add -A`.** Stage named paths. A broad add from one session is
what swept another session's edits into PR #47.

## Opening a session

Point it at the plan and name the slice. That plus the branch is enough
context:

> Read HOME-STRETCH.md and SESSIONS.md. We're doing P2a, the proxy endpoint,
> in syllabus-accounts. Branch `trace-J/proxy-endpoint`.

To isolate it so two sessions in the same repo cannot collide, add:

> Use a worktree.

That is the literal phrase. It creates an isolated checkout under
`.claude/worktrees/<name>` on its own branch, freshly based on `origin/main`,
and switches the session into it.

## What a worktree does and does not carry

Verified on 2026-09-15 against a probe worktree.

| | |
|---|---|
| Tracked source | comes with it |
| `.venv` (LectureAI) | **not needed.** The editable install resolves to whichever checkout you run from, so `~/apps/LectureAI/.venv/bin/python` run from inside a worktree imports that worktree's `intake/`, not main's |
| `node_modules` (syllabus-accounts) | **needed.** Run `npm install` once in the worktree |
| `~/.intake/syllabus/` | shared. Keys, schedule, inbox, log, Drive token are outside the repo, so every worktree sees the same profile data |
| `.claude/worktrees/` | ignored in both repos as of #49 and #11, so it never shows up in `git status` |

Two things are shared and will bite you:

- **The git stash stack is shared across all worktrees.** Never bare
  `git stash` / `git stash pop`. Use a throwaway WIP commit instead, or
  `git stash push -u -m "<tag>"` and `apply` by SHA.
- **Ports.** The live panel runs as a launchd agent from the main checkout
  (`com.maincoursemedia.syllabus.panel`). `panel-dev` in `.claude/launch.json`
  uses 5199. Two sessions both starting `panel-dev` collide, so give a second
  one a different port: `.venv/bin/python -m intake.gui --port 5200 --no-browser`.

Worktrees are not created ahead of time on purpose. One made today and used in
three weeks is based on a stale `main`; creating it at session start is always
based on current `origin/main`.

## The slices, in order

Apple gates only P7. Everything else can run now.

| # | Slice | Repo | Branch | Depends on |
|---|---|---|---|---|
| 1 | Transcriber seam + benchmark | LectureAI | `trace-J/transcriber-seam` | nothing |
| 2 | Proxy endpoint | syllabus-accounts | `trace-J/proxy-endpoint` | nothing |
| 3 | Panel calls the proxy | LectureAI | `trace-J/panel-via-proxy` | 2 merged |
| 4 | Stripe and entitlements | syllabus-accounts | `trace-J/stripe` | 2 merged |
| 5 | Study assistant | LectureAI | `trace-J/study-assistant` | 4 merged |
| 6 | Legal pages and consent | syllabus-accounts | `trace-J/legal-pages` | the lawyer |
| 7 | Security bar, worker | syllabus-accounts | `trace-J/security-worker` | 2, 4, 6 |
| 8 | Security bar, panel | LectureAI | `trace-J/security-panel` | 3, 5 |
| 9 | Signing and release | LectureAI | `trace-J/signing` | Apple |

Slice 6 no longer includes an in-app "I have permission to record"
click-through: that gate was removed from the app in 0.6.0, and the Terms
carry the user's responsibility for consent.

**1 and 2 are safe to run at the same time.** Different repos, no shared
files, no dependency. That is the one genuinely parallel pair; everything
after it wants the previous slice merged first.

**7 and 8 go last on purpose.** Auditing a surface that is still moving means
auditing it twice.

## Why the order is what it is

Slice 1 sets your per-hour cost, which sets the tier caps, which P3 in the
plan prices against. Slice 2 is the actual product change: it is what removes
"go make two AI accounts" from onboarding. Slices 7 and 8 exist because the
proxy makes you a key custodian for the first time; today you hold no API keys
and no user audio, and after slice 2 you hold both.

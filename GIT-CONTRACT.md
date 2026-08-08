# Tessera v1 — VCS / Git Strategy Contract

**Status:** Ratified (CTO sign-off, 2026-08-07)
**Binding on:** CTO (Kevin), Igor (Senior Developer), Josh (Senior QA Analyst)
**Supersedes:** any ad-hoc git habit. Deviation requires explicit CTO waiver.
**Companion docs:** `CONTRACTS.md` (build/spec contract), `formal-specifications/` (external source of truth, vendored).

---

## 1. Scope & authority

This contract governs **all git operations** for the Tessera v1 framework project:

- The framework source repository (this workspace root, `/home/company/Desktop/Workspaces/Kevin (CTO)/tessera-v1`).
- Every branch, worktree, commit, merge, and tag touching that repository.
- The behavior of Igor (build) and Josh (QA) when they execute kanban tasks against this repo.

It does **not** govern `formal-specifications/` — that is a separate upstream clone (remote `origin` =
`github.com/kazkriska/tessera-docs`) used purely as a **vendored reference**. It is git-ignored
here and never committed into the framework repo.

---

## 2. Repository topology

```
<workspace root>  (= framework repo root, git-initialized)
├── .git/                      # the framework repo
├── .gitignore                 # excludes formal-specifications/, .ticket-runtime/, .worktrees/, .venv, .env
├── CONTRACTS.md               # build/spec contract (tracked)
├── GIT-CONTRACT.md            # this file (tracked)
├── pyproject.toml             # uv-managed, requires-python >=3.12
├── lib/ticket-management/     # runtime implementation (tracked)
├── tessera/                   # SDK package (tracked)
├── tests/                     # test suite (tracked)
├── Tickets/                   # runtime data — git-ignored when present
├── Skills/                    # future resource repo — git-ignored until used
├── formal-specifications/              # vendored reference — git-ignored (separate .git)
├── .ticket-runtime/           # disposable runtime state — git-ignored
└── .worktrees/                # per-task git worktrees — git-ignored (derived)
```

- **Single source of truth for code = `main` of this repo.** Kanban board state is tracking, not source.
- **`formal-specifications/` is never edited or committed here.** Spec changes go upstream; we re-read.
- **`.ticket-runtime/`, `Tickets/`, `.worktrees/`, `.venv/`, `.env`, `__pycache__/`, `*.egg-info/`**
  are always git-ignored (per Invariant I-2 / I-5 — runtime state is disposable).

---

## 3. Branch model

### 3.1 Permanent branches
- **`main`** — the integration branch. Always buildable, always test-green, always reviewed.
  Direct commits to `main` are **forbidden** except by the CTO during merge operations.
- No other long-lived branches. Release tags are immutable pointers, not branches.

### 3.2 Feature / task branches (the only other branch type)
Every unit of execution work gets exactly one branch, named:

```
wt/<taskkey>-<short-desc>
```

- `<taskkey>` = the kanban task's human key (e.g. `a1`, `b2`, `d3`). Deterministic, human-readable.
- `<short-desc>` = kebab-case summary (e.g. `scaffold`, `manifest-loader`, `event-bus`).
- Examples: `wt/a1-scaffold`, `wt/b2-registry`, `wt/d3-runners`.

Branches are created **from `main`** (or, for a task whose dependency is still open, from that
dependency's branch after it has been merged to `main` — never from an unmerged feature branch).

### 3.3 Hotfix branches (exception path, v1 rarely used)
`hotfix/<short-desc>`, branched from the release tag, merged back to `main` with `--no-ff`.
Must carry a CTO waiver note in the merge commit body.

---

## 4. Worktree policy

Each kanban task executes in its **own git worktree**, never directly in the `main` checkout.

### 4.1 Creation (by the executing agent, at task start)
The agent creates the worktree before any code:

```bash
cd <repo-root>
git fetch --quiet origin main 2>/dev/null || true
git worktree add -b wt/<taskkey>-<short-desc> .worktrees/<taskkey> main
cd .worktrees/<taskkey>
```

- Worktree directory = `<repo-root>/.worktrees/<taskkey>/`.
- Branch name MUST match the convention in §3.2.
- **Singleton rule:** a task key maps to exactly one worktree + one branch. If the worktree already
  exists (e.g. resuming), the agent reuses it — never creates a second one for the same task.
- The agent operates **only inside** its worktree. It never touches `main`, other tasks' worktrees,
  or `formal-specifications/`.

### 4.2 During work
- Commit early, commit often (see §5). Each green milestone is a commit.
- The agent may run `uv` (venv, install, test) inside its worktree.

### 4.3 Cleanup (after CTO merge to `main`)
Once the CTO has merged the branch to `main` and verified, the worktree is removed:

```bash
cd <repo-root>
git worktree remove .worktrees/<taskkey> --force   # --force only if dirty-but-merged
git branch -d wt/<taskkey>-<short-desc>             # delete the feature branch
```

The CTO performs cleanup; agents do not delete their own branches (avoids premature loss).

### 4.4 Conflict with kanban auto-materialization
The gateway kanban dispatcher is **disabled** (`kanban.dispatch_in_gateway: false`). Therefore the
kanban board will NOT auto-create worktrees. The worktree is created **by the agent per §4.1**, not
by kanban. This is intentional and matches the contract. (If the dispatcher is ever re-enabled, this
clause takes precedence and the CTO re-disables it.)

---

## 5. Commit message standard

All commits use **Conventional Commits** (strict):

```
<type>(<scope>): <subject>

<body — optional, why not what>

Refs: <kanban-taskkey>
```

### 5.1 `<type>` (one of)
| Type | Use |
|---|---|
| `feat` | new capability / module |
| `fix` | bug fix |
| `docs` | docs only (README, CONTRACTS, comments-as-docs) |
| `refactor` | restructure without behavior change |
| `test` | add/extend tests |
| `perf` | performance change |
| `build` | build/packaging/dependency (pyproject, uv) |
| `ci` | CI/config automation |
| `chore` | misc, no behavior (gitignore, formatting) |

### 5.2 `<scope>` (module the change touches)
One of: `manifest`, `registry`, `watcher`, `bus`, `scheduler`, `dispatcher`, `executor`, `state`,
`env`, `perms`, `lifecycle`, `cli`, `sdk`, `repo`, `docs`, `tests`.
If a change spans scopes, pick the dominant one; do not use `*`.

### 5.3 `<subject>`
- Imperative, lowercase first word ("add", "fix", "enforce"), no trailing period.
- ≤ 72 characters.
- Describes the change, not the task title.

### 5.4 `<body>` (optional but required for non-trivial commits)
- Explain **why**, not **what** (the diff shows what).
- Wrap at 80 columns.

### 5.5 `Refs:` footer
- **Mandatory.** One line: `Refs: <taskkey>` (e.g. `Refs: a1`, `Refs: d3`). Links commit → kanban task.

### 5.6 Forbidden
- No `--no-verify` to skip hooks.
- No committing `.env`, secrets, `.ticket-runtime/`, `Tickets/` data, or `formal-specifications/`.
- No `wip` / `temp` / empty subjects. Squash or amend local junk before merge — never merge a `wip` commit.
- No merge commits inside a feature branch (rebase onto latest `main` instead; see §6.1).

### 5.7 Examples (good)
```
feat(manifest): reject YAML anchors and aliases in loader

PyYAML safe_load silently resolves anchors, so the ban must be enforced by
scanning the event stream for AnchorToken/AliasEvent before load.

Refs: a2
```
```
fix(executor): block path traversal outside ticket root

Runner now sanitizes path args; ../ escapes were possible when a hook
passed an absolute path. Adds regression test.

Refs: d3
```

---

## 6. Merge & integration

### 6.1 Before merge (agent responsibilities)
- Rebase the feature branch onto current `main`: `git rebase main` (resolve conflicts in-branch).
- Ensure `uv run pytest` is green in the worktree.
- Ensure no lint/type errors (ruff if adopted).

### 6.2 Merge (CTO-only gate)
The CTO merges after **independent verification** (re-run tests, inspect diff) and, for build tasks,
after Josh's QA sign-off:

```bash
git checkout main
git merge --no-ff wt/<taskkey>-<short-desc> -m "merge(<scope>): <taskkey> <title>

Refs: <taskkey>
Summary: <one-line>
QA: <josh sign-off or 'n/a for docs/test tasks'>"
```

- **`--no-ff` is mandatory** — preserve the feature-branch history and the merge point. Squash merges
  are forbidden (we want the audit trail; Spec Status discipline depends on it).
- The CTO, not the agent, performs the merge and writes the merge commit body.

### 6.3 Review gate (the lifecycle)
```
Igor builds (branch wt/X)  →  CTO verifies build independently
   →  Josh QA on same worktree (branch wt/X)  →  CTO verifies QA independently
   →  CTO merges wt/X → main (--no-ff)  →  CTO removes worktree/branch
```
No branch reaches `main` without the CTO's explicit merge. Igor/Josh never merge to `main`.

### 6.4 Tagging
- Releases tagged `vMAJOR.MINOR.PATCH` (semver) on `main`, annotated:
  `git tag -a v0.1.0 -m "Tessera v0.1.0 — Phase A–H baseline"`.
- v1.0.0 only after all Phases A–H complete and the test suite is green end-to-end.

---

## 7. Role responsibilities

| Role | Git duties |
|---|---|
| **CTO (Kevin)** | Owns `main`; creates board/tasks; verifies builds & QA independently; performs all merges (`--no-ff`); tags releases; removes worktrees/branches; enforces this contract. |
| **Igor** | Creates his worktree per §4.1; commits per §5; rebases onto `main`; keeps `pytest` green; **never** touches `main` or other tasks' trees; does not merge. |
| **Josh** | Same worktree discipline as Igor for QA branches; adds/extends tests; **never** touches `main`; does not merge. |

---

## 8. Enforcement

- This contract is the standard the CTO checks during independent verification.
- A commit or merge violating §3–§6 is rejected at review; the CTO returns the work to the agent
  with the specific clause cited (matching the CTO profile's "constructive on review" rule).
- Ambiguities discovered in practice are resolved here by the CTO; the doc is amended and the
  resolution noted in the task's `activity.jsonl`/kanban comment — never silently worked around.

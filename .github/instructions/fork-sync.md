# Fork Sync Strategy

Date: 2026-05-24

## Overview

This repo is a fork of `Beever-AI/beever-atlas`. Custom commits sit on top of the last upstream commit, forming a clean linear history. The upstream remote is already configured.

## Commit Structure

```
upstream:  ... → <last-upstream-commit>
this fork: ... → <last-upstream-commit> → [custom commit 1] → [custom commit 2] → HEAD
```

Custom commits must always remain stacked on top of the latest upstream commit.

## Syncing Upstream Changes (Rebase Workflow)

Do NOT use `git merge` or GitHub's "Sync fork" button — these create merge commits that tangle fork history with upstream.

Use rebase:

```powershell
# 1. Fetch latest upstream commits
git fetch upstream

# 2. Replay your custom commits on top of the new upstream base
git rebase upstream/main

# 3. If a conflict appears, resolve it in the affected file, then:
git add <conflicted-file>
git rebase --continue

# 4. Push your fork (force-push is required after rebase)
git push origin main --force-with-lease
```

Use `--force-with-lease` rather than `--force`. It refuses to overwrite if the remote was updated by someone else since your last push.

## Resolving Conflicts

Conflicts occur only on lines that both you and upstream changed. When resolving:

- Keep upstream's structural changes (new imports, refactors, renamed functions).
- Re-apply your fork-specific logic on top.
- If a conflict is in an upstream-owned file (`src/`, `web/`, `bot/`), prefer moving your change into the plugin layer afterward so future rebases are conflict-free.

## Reducing Future Conflicts

The fewer upstream-owned files your commits touch, the fewer conflicts you will see on each rebase. Aim to:

- Move changes in `src/` into monkey patches inside `plugins/` (see `plugin-mode.md`).
- Move changes in `web/` into plugin-owned React components or route extensions.
- Avoid editing `pyproject.toml` upstream dependencies directly; add fork-only deps under a clearly marked comment block.

## After Every Rebase

1. Run plugin smoke tests to confirm patches still apply.
2. Verify `start_with_plugins.py` loads all plugins without error.
3. Check `plugins/loader.py` for any import paths broken by upstream refactors.

## Agent Procedure: Selective Upstream Sync

When asked to sync upstream changes, follow this procedure exactly. Do not ask the user which option to use — execute the steps below autonomously and report what was done.

### Step 1 — Fetch and classify

```powershell
git fetch upstream

# Files changed in upstream since the fork point
git diff --name-only main upstream/main > $env:TEMP\upstream_changed.txt

# Files touched by fork-only commits (everything between fork point and HEAD)
git diff --name-only $(git merge-base main upstream/main) main > $env:TEMP\fork_changed.txt
```

Read both lists. Classify every file into one of three categories:

| Category | Condition | Default action |
|---|---|---|
| **Upstream-only** | In upstream list, NOT in fork list | Accept upstream automatically |
| **Fork-only** | In fork list, NOT in upstream list | Keep fork version, no action needed |
| **Contested** | In BOTH lists | Apply ownership rules (Step 2) |

### Step 2 — Apply ownership rules to contested files

For each contested file, determine its owner using the path:

| Path prefix | Owner | Conflict resolution |
|---|---|---|
| `plugins/` | Fork | Keep fork (`--ours`) |
| `notes/` | Fork | Keep fork (`--ours`) |
| `scripts/` | Fork | Keep fork (`--ours`) |
| `.github/instructions/` | Fork | Keep fork (`--ours`) |
| `src/` | Upstream | Accept upstream then re-apply fork additions (see Step 3) |
| `web/` | Upstream | Accept upstream then re-apply fork additions (see Step 3) |
| `bot/` | Upstream | Accept upstream then re-apply fork additions (see Step 3) |
| `pyproject.toml` | Shared | Accept upstream structure, preserve fork-only dependency lines (see Step 3) |
| `uv.lock` | Upstream | Accept upstream unconditionally (`--theirs`) |
| `web/package-lock.json` | Upstream | Accept upstream unconditionally (`--theirs`) |

### Step 3 — Handle upstream-owned contested files

For each file owned by upstream but also modified by the fork:

1. Show the fork's diff on that file:
   ```powershell
   git diff $(git merge-base main upstream/main) main -- <file>
   ```
2. Show the upstream's diff on that file:
   ```powershell
   git diff $(git merge-base main upstream/main) upstream/main -- <file>
   ```
3. Read both diffs. Identify which hunks are fork-specific (feature additions, plugin hooks, custom logic) versus which are structural upstream changes (renamed symbols, new imports, refactored functions).
4. Apply upstream's version as the base:
   ```powershell
   git checkout --theirs -- <file>
   ```
5. Manually re-apply only the fork-specific hunks on top of the upstream base. Add the result and continue.
6. If the fork change in this file is a patch that belongs in `plugins/` instead, note it in your report and skip re-applying it — leave the upstream version clean and flag it for migration.

### Step 4 — Run the rebase

```powershell
git rebase upstream/main
```

For each conflict that surfaces during the rebase:

- Determine the file's owner using the path table in Step 2.
- For fork-owned files: `git checkout --ours -- <file> && git add <file>`
- For upstream-owned files: apply Step 3 logic, then `git add <file>`
- For lockfiles: `git checkout --theirs -- <file> && git add <file>`
- Then: `git rebase --continue`

If a fork commit becomes empty after resolution (upstream already made the same change), run:
```powershell
git rebase --skip
```

### Step 5 — Verify

```powershell
# Confirm rebase completed and history is linear
git log --oneline -10

# Show remaining intentional divergences from upstream
git diff upstream/main --stat

# Run plugin loader check
python -c "from plugins.loader import load_plugins; print('loader ok')"
```

Report any files that still diverge from upstream. For each divergence, state whether it is intentional (fork feature) or a candidate for plugin migration.

### Step 6 — Push

```powershell
git push origin main --force-with-lease
```

### Agent report format

After completing the sync, report:

```
Upstream sync complete.

Upstream-only files applied:   <count> files
Fork-only files untouched:     <count> files
Contested files resolved:      <count> files
  - Kept fork version:         <list>
  - Accepted upstream version: <list>
  - Manually merged:           <list>
  - Flagged for plugin migration: <list>

Commits skipped (already in upstream): <count>
Plugin loader: ok / FAILED (details)
```

---

## Recovery: GitHub "Sync Fork" Already Clicked

If the GitHub "Sync fork" button was already used, `origin/main` on GitHub now contains upstream commits merged in via a merge commit. Your local `main` is still clean and unaffected.

**Diagnose first:**

```powershell
git fetch origin
git fetch upstream
git merge-base HEAD upstream/main   # should equal your fork point commit
git log --oneline HEAD -8           # confirms your local commits are still intact
```

If your local `main` still has your custom commits on top of the old fork point, the fix is straightforward:

```powershell
# 1. Rebase your local commits onto the latest upstream
git rebase upstream/main

# 2. Resolve any conflicts using the ownership rules in Step 2/3 above

# 3. Force-push to overwrite the messy origin/main on GitHub
git push origin main --force-with-lease
```

After this, `origin/main` will be restored to a clean linear history with your commits stacked on top of the latest upstream. The merge commit created by GitHub is gone.

**If your local main was already pulled from origin (merge commit is now local):**

```powershell
# Reset local main back to your last custom commit before the merge
git log --oneline    # find the last commit that is yours (not upstream or a merge commit)
git reset --hard <your-last-custom-commit-sha>

# Then rebase and push as above
git rebase upstream/main
git push origin main --force-with-lease
```

---

## What to Avoid

| Action | Why to avoid |
|---|---|
| `git merge upstream/main` | Creates a merge commit; pollutes fork history |
| GitHub "Sync fork" button | Performs a merge under the hood |
| Manual file-by-file copying | Error-prone; bypasses git conflict detection |
| Editing upstream files without a plugin alternative | Increases conflict surface on every future rebase |

## Remote Configuration Reference

```
origin    https://github.com/albertje8118/beever-atlas  (your fork)
upstream  https://github.com/Beever-AI/beever-atlas.git (source of truth)
```

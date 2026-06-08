---
name: javahost-pr-workflow
description: >
  How to ship a body of work on the JavaHost plugin as a series of focused,
  independently-revertable PRs — one feature per branch, concern-split commits,
  a MANDATORY code-review gate before every merge, CI + live verification, and a
  per-PR release. Use when planning or executing multi-feature work ("ship these
  as PRs", "do this in cherry-pickable PRs", "open a PR for X"), or before
  merging anything. Pairs with javahost-dev (conventions), javahost-release
  (tagging), javahost-security (review checklist), and javahost-parallel-review
  (fleet review of many PRs at once).
allowed-tools: Bash(make test) Bash(make lint) Bash(git *) Bash(gh *) Bash(node --check *) Bash(python3 -m pytest *) Bash(python3 -m py_compile *) Bash(python3 -m bandit *)
---

# JavaHost — PR workflow (the merge gate is non-negotiable)

Ship work as **small, focused PRs**, each its own minor release, each cleanly
revertable. **No PR merges without a recorded code review.** This is the rule the
rest of the skill exists to support.

## 1. Plan the slices
- Decompose the work into **N focused PRs**, one coherent feature each (e.g.
  "log rotation", "JDK update detection"). Smaller = easier to review and revert.
- Order them: a shared foundation PR first; later PRs build on merged main.
- Track them (TaskCreate) and mark `blockedBy` where one depends on another.
- For genuinely independent slices, develop/review them **in parallel** — see
  the `javahost-parallel-review` skill.

## 2. Branch + commit
- `git checkout main && git pull && git checkout -b feat/<slug>` (or `fix/…`).
- **Split commits by concern** so each is cherry-pickable:
  `backend → frontend → tests → docs → chore(release)`. Conventional Commits.
- End every commit message with the repo's `Co-Authored-By` trailer.

## 3. Verify locally (all must pass)
```
make test            # py_compile + pytest (offline, stdlib-only)
make lint            # shellcheck / py_compile
python3 -m bandit -c .bandit -ll -ii -q <changed .py>      # Medium+ clean
# inline-JS syntax — STRIP HTML COMMENTS FIRST or it false-fails:
python3 - <<'PY' ; node --check /tmp/jh.js
import re;t=re.sub(r'<!--.*?-->','',open('plugin/javahost/index.html').read(),flags=re.S)
open('/tmp/jh.js','w').write('\n;\n'.join(re.findall(r'<script[^>]*>(.*?)</script>',t,flags=re.S)))
PY
```
`tests/test_ui_a11y.py` (in the suite) is the authoritative UI check.

## 4. THE REVIEW GATE — required before merge
Every PR gets an **adversarial code review** before it closes. Pick one:
- **Single PR:** run `/code-review high` on the branch diff (or spawn a reviewer
  subagent). For any change that runs commands, downloads, extracts archives,
  writes services/cron, or touches secrets/config, **also** apply the
  `javahost-security` checklist.
- **Many PRs at once:** use `javahost-parallel-review` (a fleet: one reviewer per
  PR → adversarial verify each finding → integration pass).

Then **triage every confirmed finding**: fix it, or record an explicit, reasoned
accept in the PR thread. Re-run the local verify after fixes. Do **not** merge
with unresolved confirmed findings of `medium`+ severity.

## 5. Open the PR + CI gate
- `git push -u origin <branch>` then `gh pr create` with a body that states
  *what / why / tests / rollback*.
- Wait for **CI green** (`gh run watch`). Never merge red.

## 6. Live verification (behavioral changes)
If the change has runtime behavior the unit tests can't fully prove (signals,
cron writes on a hardened host, network fetches, service lifecycle, UI flows),
verify on a real panel before/just-after merge — see `javahost-test-deploy`:
```
make deploy VPS_HOST=<user@host>      # rsync + bt restart (host from your private ops notes)
```
Then run the plugin's stdlib backend directly on the box (it imports `core.*`
without the panel) to exercise the real path, and a Playwright pass for UI.
**Never put a panel hostname/IP/domain in committed code, docs, or screenshots.**

## 7. Merge + release (per PR)
- `gh pr merge <n> --squash --delete-branch` → one squash commit per feature.
- Tag the release on main and verify the Release workflow — use `javahost-release`
  (bump `info.json` + `CHANGELOG.md`, `chore(release): vX.Y.Z`, push tag).

## 8. Rollback
- One squash-merge per PR ⇒ `git revert <merge-sha>` removes exactly one feature.
- Concern-split commits ⇒ cherry-pick or revert a single piece in isolation.
- No PR may depend on a *later* one; only on already-merged main.

## Gotchas (learned the hard way)
- `index.html` is one ~500-char-per-line file; BRE `grep -n '…\|…'` flakes on it
  → search with **python** or `grep -F`.
- The aaPanel `get` is an **attribute namespace** (`panel.attr` = `getattr`), not
  a dict → endpoint tests pass a `SimpleNamespace`, never `{...}`.
- A managed `/etc/cron.d/*` writer must **only relock `chattr +i` if the dir was
  already immutable** — never harden a dir that wasn't (use `util.immutable.writable`
  in Python; check `was_locked` in shell).
- After `make deploy`, the panel **must** be restarted (`make deploy` does it) or
  it serves the stale module.

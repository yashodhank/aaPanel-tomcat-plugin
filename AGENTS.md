# aaPanel Tomcat Plugin — Agent Rules (auto-loaded every session)

> **If you're working on this project, do NOT proceed before reading this file.**
> MEMPALACE: Query `mempalace_search(query="javahost", wing="javahost")` before starting.

## Mandatory workflow

1. **PR gate (non-negotiable):** Every change ships as a PR off a feature branch.
   - Branch: `feat/<slug>` or `fix/<slug>` from main
   - Commits: concern-split (backend → frontend → tests → docs → chore)
   - Conventional Commits format, `Co-Authored-By` trailer on every commit
   - **NO direct commits to main.**
   Load `skill: "javahost-pr-workflow"` before starting any implementation work.

2. **Security review:** Every PR goes through `skill: "javahost-security"` checklist.
   Applies to any change touching: shell, downloads, archive extraction, filesystem,
   secrets, service units, Tomcat hardening, proxy config, or DB credentials.

3. **Code review gate:** Every PR gets adversarial code review BEFORE merge.
   Single PR → use `skill: "caveman-review"` on the branch diff.
   Many PRs → use `skill: "javahost-parallel-review"`.

4. **Release per PR:** After merge, bump version, update CHANGELOG, tag.
   Use `skill: "javahost-release"` — never release without this skill loaded.

## Code conventions (enforced)

- Commands: `core.util.shell.run([...])` arg-lists only, NO `shell=True`
- Input: validated via `core.util.validate` before touching fs/shell/templates/URLs
- Downloads: `util.download.fetch_verified` (SHA-512 + GPG, fail-closed)
- Archives: `deploy.war.safe_extract` (zip-slip-safe)
- Filesystem: `fs.atomic_write` + `fs.safe_rmtree` (marker/root-gated)
- Panel coupling: ONLY `core/compat/aapanel.py` touches aaPanel internals
- Secrets: DB creds in `app.env` (0640) only, never in URL/logs/response
- Never commit: `5d.bisotech.in`, VPS IPs, panel creds, aaPanel proprietary source
- Templates: `@@token@@` via `tomcat.templating`

## Project skills (in `.claude/skills/`)

| Skill | When to use |
|-------|------------|
| `javahost-dev` | Conventions, build/test/deploy runbook |
| `javahost-pr-workflow` | Before creating PRs or branching |
| `javahost-security` | Security review before merge |
| `javahost-release` | Cutting a release |
| `javahost-parallel-review` | Reviewing multiple PRs at once |
| `javahost-test-deploy` | Deploying to test VPS |
| `javahost-teardown` | Cleaning test VPS state |
| `javahost-ui` | Editing `index.html` (WCAG 2.2 AA + WAI-ARIA) |

## Memory system

- **mempalace (`wing: "javahost"`, 6 drawers):** Project decisions, architecture,
  conventions, deployment state. Query before starting, write after learning.
- **graphify:** Code knowledge graph. Use `graphify_query_graph` for code structure
  questions, `graphify_list_prs` to check open PRs before starting work.

# Contributing to JavaHost

Thanks for your interest. JavaHost is an independent, Apache-2.0, **clean-room**
plugin. The single most important rule below is non-negotiable; everything else
is ordinary OSS workflow.

## The clean-room rule (read first)

JavaHost is **not** a fork of aaPanel's proprietary `tomcat2` plugin and must
never become one.

- **NEVER** paste, copy, or adapt aaPanel / BaoTa source code, UI layout
  (`index.html` structure), icons, images, strings, or any other panel assets
  into this repository — in code, comments, commits, issues, or PRs.
- Build **only** against the panel's public, documented third-party plugin API.
  In JavaHost, all panel coupling is isolated to `core/compat/aapanel.py`, which
  uses public helpers (`public.returnMsg`, `public.GetMsg`, `public.WriteLog`).
  Do not introduce panel-internal calls anywhere else.
- The panel vendor's (AAPANEL/BaoTa) proprietary license is what governs *their*
  code. Its terms permit independently-developed plugins to interoperate via the
  public plugin API (§3.1) while prohibiting copying/derivation of their source
  and assets (the §2.2 / §4.3 restriction context). Respecting that boundary is
  precisely what keeps JavaHost shippable under Apache-2.0. This repository's own
  code is Apache-2.0 (see `LICENSE` / `NOTICE`).
- Third-party runtimes (Tomcat, Temurin/OpenJDK, JDBC drivers) are downloaded and
  integrity-verified at runtime — never vendored into the repo.

CI enforces part of this automatically: it fails if a proprietary-derived file
(e.g. `tomcat2_main.py`) is ever tracked.

If you are unsure whether something crosses the line, do not commit it — open an
issue and ask first.

## Dev setup

No panel is required for development; the test suite runs offline.

```bash
make test     # py_compile every plugin .py, then run pytest (tests/)
make lint     # shellcheck the *.sh hooks + py_compile
```

Optional, against a panel **you control**:

```bash
make deploy VPS_HOST=root@your-server   # rsync + restart your panel
make zip                                # build javahost.zip locally
```

Keep new logic in `core/` (portable, unit-testable) and keep `javahost_main.py`
thin. Validate all external input through `core/util/validate.py`, build commands
as argument lists via `core/util/shell.run` (never `shell=True`), and use
`core/util/fs` for writes/removals so the managed-marker and managed-root
guardrails stay intact.

## Branch + PR workflow

1. Fork (or branch off `main` if you have access). Do not commit directly to
   `main`.
2. Make focused changes with tests where practical.
3. Run `make lint && make test` locally before pushing — CI runs the same checks.
4. Open a PR against `main`. Describe the change and confirm it adds no aaPanel
   source/UI/assets.
5. Address review; squash/clean history as asked.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(optional scope): short summary

optional body explaining the why
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`.
Examples:

```
feat(tomcat): resolve latest 11.x patch dynamically
fix(deploy): reject symlink entries during WAR extraction
docs: add offline tomcat install instructions
chore: release v0.1.0
```

## Reporting bugs / requesting features

Open an issue with steps to reproduce, expected vs. actual behaviour, and
relevant log output (scrub any secrets — JavaHost never logs credentials, and
neither should issue reports). For security issues, follow
[`SECURITY.md`](SECURITY.md) instead of opening a public issue.

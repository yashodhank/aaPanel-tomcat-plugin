---
name: javahost-release
description: >
  Release runbook for the JavaHost plugin — pick the next SemVer, sync info.json,
  conventional-commit, tag vX.Y.Z, push, and verify the GitHub Actions CI + Release
  workflows and the published release assets. Use when cutting or publishing a new
  version of the plugin.
disable-model-invocation: true
argument-hint: "[major|minor|patch|X.Y.Z]"
---

# Cut a JavaHost release

Versioning: **SemVer**. Commits: **Conventional Commits** (`feat:`, `fix:`, `docs:`,
`ci:`, `chore:`, `refactor:`, `test:`). The release workflow triggers on tags `v*`.

## Steps
1. **Green first.** `make test && make lint` must pass; working tree clean.
2. **Choose version** from `$ARGUMENTS` (or bump from `plugin/javahost/info.json`'s
   `versions`): major = breaking, minor = new feature, patch = fix only.
3. **Update CHANGELOG.md** — add a dated `## [X.Y.Z]` section (Keep a Changelog
   style: Added/Changed/Fixed/Security) summarizing changes since the last tag
   (`git log --oneline <lastTag>..HEAD`).
4. **Sync `plugin/javahost/info.json`** `versions` (the release workflow also does
   this, but keep the repo honest).
5. **Commit**: `chore(release): vX.Y.Z` (+ the CHANGELOG/info.json changes).
6. **Push main**, then **tag + push**:
   ```bash
   git push origin main
   git tag -a vX.Y.Z -m "JavaHost vX.Y.Z — <one-line summary>"
   git push origin vX.Y.Z
   ```
7. **Verify** (do not declare done until green):
   ```bash
   gh run list --limit 4
   gh release view vX.Y.Z --json tagName,name,assets \
     --jq '{tag:.tagName,name:.name,assets:[.assets[].name]}'
   ```
   Expect assets `javahost-vX.Y.Z.zip` + `.zip.sha256`, CI + Release both success.

## Guardrails
- Never tag if `make test` is red.
- The release zip is built from `plugin/javahost/` only (see `.github/workflows/release.yml`); confirm no stray files.
- If a release is wrong, delete the tag/release (`gh release delete`, `git push --delete origin vX.Y.Z`) and re-cut — don't force-mutate a published asset.

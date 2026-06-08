---
name: javahost-parallel-review
description: >
  Review many JavaHost PRs (or feature branches) at once with a parallel fleet of
  reviewer agents — one reviewer per PR, an adversarial verify pass that refutes
  weak findings, and a cross-PR integration pass. Use when asked to "review all
  the PRs", "fleet/parallel review", "review these branches before merge", or to
  audit a batch of merged PRs retrospectively. This fans out many agents and is
  billed — only run it when the user opts into multi-agent orchestration. The
  per-PR gate itself lives in javahost-pr-workflow.
disable-model-invocation: true
allowed-tools: Bash(gh *) Bash(git *)
---

# JavaHost — parallel fleet review

Audit a batch of PRs/branches concurrently. The shape is **find → verify →
integrate**: a reviewer per PR finds defects, independent verifiers try to
**refute** each finding (so only real ones survive), and one agent checks how the
PRs interact. This is a `Workflow` call — it spawns a fleet, so confirm the user
wants it.

## When to use
- A stack of open PRs to clear before merging.
- A just-shipped batch of merged PRs to audit retrospectively (what we did for
  v0.21–v0.26).
- Any time "review them all in parallel" is the ask.

## How to run
1. Collect the targets: `gh pr list --state open` (or `--state merged --limit N`).
2. For each, write a one-line **scope** (what the PR changed) so the reviewer has
   focus without re-deriving it.
3. Launch a `Workflow` shaped like the script below. It `pipeline()`s over the
   PRs so each PR's findings verify as soon as its review lands (no barrier), then
   runs a single integration agent.
4. **Triage the result** yourself: fix confirmed `medium`+ findings (as their own
   `fix:` PR through `javahost-pr-workflow`), record reasoned accepts for the rest.

## Reviewer focus (what each agent hunts)
Real defects only, cite `file:func/line`: correctness/logic, security (path
traversal, shell/command injection, **killpg targeting the wrong pgid**, untrusted
input, secret/`config.json` perms leakage), races/TOCTOU (copy-truncate log loss,
cancel-vs-finalize, cache R/W), regressions, **clean-room/convention** violations
(non-stdlib import, aaPanel-derived code, non-CSP-safe JS, missing `validate.*`),
a11y (color-only signal, missing label/role, focus-trap gaps), resource leaks,
error-swallowing. **No style nits.** Empty findings if clean.

## Verifier (adversarial)
Default `real=false`; flip to true only after reading the actual code (and a quick
`python -c` repro where useful). Adjust severity. One-paragraph reason citing code.

## Integration pass (cross-PR)
Look for problems no single-PR review can see: e.g. two managed `/etc/cron.d`
writers racing the same dir's immutable bit; uninstall cleaning one cron but
orphaning another; a cache writer and a `config.set()` writing the same file; a
modal/wizard reading `STATE` before `GetStatus` resolves; endpoint-name
collisions; `GetStatus` payload bloat; duplicated logic across new modules.

## Script skeleton (adapt the PR list + scopes)
```js
export const meta = { name:'review-prs', description:'Fleet review of JavaHost PRs',
  phases:[{title:'Review'},{title:'Verify'},{title:'Integrate'}] }
const REPO='/path/to/aaPanel-tomcat-plugin'
const PRS=[ {n:1, scope:'…'}, /* … */ ]
const FINDINGS={type:'object',properties:{pr:{type:'number'},findings:{type:'array',items:{type:'object',
  properties:{severity:{enum:['blocker','high','medium','low','nit']},category:{type:'string'},
  title:{type:'string'},file:{type:'string'},location:{type:'string'},detail:{type:'string'},
  suggestion:{type:'string'}},required:['severity','category','title','file','detail']}}},required:['pr','findings']}
const VERDICT={type:'object',properties:{real:{type:'boolean'},severity:{type:'string'},reason:{type:'string'}},required:['real','severity','reason']}
phase('Review')
const out = await pipeline(PRS,
  pr => agent(`Senior reviewer. cd ${REPO}; gh pr diff ${pr.n}; read touched files in context. PR#${pr.n}: ${pr.scope}. Find REAL defects only.`,
              {label:`review:PR#${pr.n}`, phase:'Review', schema:FINDINGS}),
  (rev,pr) => parallel(((rev&&rev.findings)||[]).map(f => () =>
     agent(`Adversarially REFUTE this finding in PR#${pr.n} (repo ${REPO}); read the real code. ${JSON.stringify(f)}`,
           {label:`verify:PR#${pr.n}`, phase:'Verify', schema:VERDICT}).then(v=>({...f,pr:pr.n,verdict:v})))))
const confirmed = out.flat().filter(Boolean).filter(x=>x.verdict&&x.verdict.real)
phase('Integrate')
const integration = await agent(`Cross-PR integration review of ${REPO}; how do these PRs interact? ${PRS.map(p=>`PR#${p.n}: ${p.scope}`).join('; ')}`, {phase:'Integrate'})
return { confirmed_count:confirmed.length, confirmed, integration }
```

Keep the gate honest: a green CI is necessary but **not sufficient** — a PR is
done only after its findings are triaged.

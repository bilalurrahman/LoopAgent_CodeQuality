# Plan: Code-Quality / Analyzer Loop for glm-loop

## Context

The glm-loop is deployed and working against `HattanMedicalHistory` as a **build
guard** (gate = `dotnet build -warnaserror`). On a repo that already builds
clean, that gate almost never has work to do — every nightly run reports
"nothing to do." Useful as a safety net, but not a *practical* loop.

This plan converts the loop into a **code-quality / analyzer loop**: enable
nullable reference types + Roslyn analyzers + code-style rules, then have the
loop steadily fix the warnings they surface — a few each run — opening a
reviewable PR every time. It turns the loop into an autonomous tech-debt
reducer that always has real, valuable work.

Target repo today: net10.0, 4 projects (~79 .cs files), builds warning-clean
with default analysis, **no** `.editorconfig`, **no** analyzer config, an EF
Core `Infrastructure/Migrations` folder (generated — never touch). Intended to
be reusable on future .NET projects.

## Core design problem & solution

If we enable nullable + all analyzers with `-warnaserror`, the build fails with
potentially hundreds of warnings across the codebase. A binary "build must be
clean" gate can't be satisfied in one bounded run (MAX_ITERS=5 × MAX_TURNS=40),
so the loop would escalate every night with zero progress.

**Solution — a stateless, baseline-relative warning ratchet:**

- Enable analyzers as **warnings, not errors** (build still succeeds, emits N warnings).
- The gate is a wrapper script that counts warnings and requires the working
  tree to have **at least `STEP` fewer warnings than the freshly-cloned base branch**.
- No stored counter to maintain: the **base branch's current warning count is the
  state.** Each merged PR lowers the base count, so the next run's target
  tightens automatically — until the count reaches zero.
- `STEP` (e.g. 10) bounds how much each run must fix, keeping runs inside the
  turn/context budget while guaranteeing steady progress.

Flow per run:
```
clone base branch (e.g. `quality`)  ->  build, count warnings = W0  (baseline)
target = W0 - STEP  (or 0 if W0 <= STEP)
loop asks GLM to fix warnings until:  current_count <= target   (gate green)
=> commit + push glm_quality/<runid>  ->  PR into `quality`
you review + merge  ->  `quality` warning count drops  ->  next run tightens
```

## What the loop actually checks (concrete)

Three families of checks are enabled; the loop fixes what they surface.

**1. Nullable reference warnings (`CS86xx`) — highest value; catch potential
`NullReferenceException`s at compile time:**
- `CS8602` — dereference of a possibly-null value (e.g. `patient.Documents.Count`
  when `patient` may be null)
- `CS8618` — non-nullable property/field never initialized (e.g.
  `public string Name { get; set; }` → could be null at runtime)
- `CS8603` — method may return null despite a non-nullable return type
- `CS8604` — passing a possibly-null value where non-null is expected
- Typical fix: add proper annotations / defaults / guards, e.g.
  `public string Name { get; set; } = string.Empty;` or `public string? Name`.

**2. Roslyn code-quality analyzers (`CAxxxx`) — correctness / performance /
reliability / security:**
- `CA2000` — object not disposed (Stream/HttpClient resource leaks)
- `CA1849` — blocking call inside an `async` method (should `await`)
- `CA1305` / `CA1310` — string format/compare without a culture (locale bugs —
  relevant for medical data)
- `CA1822` — instance method that could be `static`

**3. Code-style rules (`IDExxxx`, via `EnforceCodeStyleInBuild`) — cleanliness:**
- `IDE0005` — unused `using` directives
- `IDE0051` / `IDE0052` — unused private members (dead code)
- `IDE0044` — fields that could be `readonly`
- `IDE0060` — unused parameters

**Explicitly out of scope (the loop must NOT do):** change behavior or public API
signatures to clear a warning; suppress with `!` (null-forgiving) or
`#pragma warning disable`; lower rule severity; or touch `Migrations/`, auth, or
EF model config. Every fix is small, mechanical, and delivered as a reviewable PR.

**Recommended first step before building anything:** run a read-only analysis —
build the repo with these analyzers switched on and produce the real warning
list (counts per rule) for HattanHealthTracker. That shows the exact backlog the
loop would work through and helps tune the initial rule set and `STEP`.

## Prerequisites (reuse — already built)

- minikube + Docker Desktop, glm-loop image (`glm-loop:latest`), `glm-loop-git`
  Secret (fine-grained PAT), `host.docker.internal:11434` Ollama networking.
- `loop.sh` already supports `LOOP_GATE_CMD`, `LOOP_SKILL_FILE`,
  `LOOP_BRANCH_PREFIX`, `LOOP_PR_BASE`, `GIT_BRANCH` overrides — no core changes.
- Scratchpad artifacts to base new files on: `Dockerfile-A`, `entrypoint-A.sh`,
  `loop-A.sh`, `cronjob-hattan.yaml`, `build-A.sh`.

## Implementation

### Phase 1 — One-time seed commit (on a dedicated `quality` branch)

Create `quality` off `dev` so quality churn never lands directly on `dev`
(you merge `quality` -> `dev` when happy). Seed these files, commit, push:

1. **`Directory.Build.props`** (repo root or `backend/`): applies to all projects
   ```xml
   <Project>
     <PropertyGroup>
       <Nullable>enable</Nullable>
       <AnalysisLevel>latest-recommended</AnalysisLevel>
       <EnforceCodeStyleInBuild>true</EnforceCodeStyleInBuild>
       <!-- warnings, NOT errors, so the build still succeeds and we can count -->
       <TreatWarningsAsErrors>false</TreatWarningsAsErrors>
     </PropertyGroup>
   </Project>
   ```
   Start conservative — nullable + `latest-recommended` only. Widen to
   `latest-all` later once the count is low.

2. **`.editorconfig`** — curated rule severities, and critically **exclude
   generated/migration code** so EF migrations don't flood the count:
   ```ini
   [*.cs]
   dotnet_analyzer_diagnostic.severity = warning
   [**/Migrations/**.cs]
   generated_code = true
   dotnet_analyzer_diagnostic.severity = none
   ```

3. **`.glm-loop/gate.sh`** — the ratchet gate (pure comparison; baseline passed in via env):
   ```bash
   #!/usr/bin/env bash
   set -uo pipefail
   SLN=backend/HattanHealthTracker.sln
   dotnet build "$SLN" --nologo 2>&1 | tee /tmp/b.out >/dev/null
   count=$(grep -cE ': warning [A-Z]+[0-9]+' /tmp/b.out)
   target="${WARN_TARGET:-0}"
   echo "warnings=$count target=$target"
   [ "$count" -le "$target" ]   # exit 0 if at/under target
   ```

### Phase 2 — Baseline computation in the entrypoint

Extend the container entrypoint (from `entrypoint-A.sh`) so that, after cloning
the base branch, it builds once, counts warnings `W0`, and exports
`WARN_TARGET = max(0, W0 - STEP)` before running `loop.sh`. This is what makes
the ratchet stateless. `STEP` comes from env (`WARN_STEP`, default 10).

### Phase 3 — Quality-specific skill

New **`skills/quality-fix.md`** (used via `LOOP_SKILL_FILE`), rules e.g.:
- Fix nullable/analyzer warnings with the **smallest correct change**: add
  proper nullable annotations / null guards; do **not** silence with `!`
  (null-forgiving), `#pragma warning disable`, or by lowering severity.
- Never change public API signatures or runtime behavior to clear a warning.
- **Never touch**: `**/Migrations/**`, auth/identity, EF model config, `*.Job`.
- Prefer fixing whole files cleanly over scattering partial fixes.
- Escalate (write to STATE.md, skip) any warning whose correct fix is ambiguous
  or would require a behavioral/design decision.

### Phase 4 — Image + CronJob

- **Image**: rebuild `glm-loop:latest` (via `build-A.sh` pattern) with the new
  entrypoint (baseline step) and bake in `skills/quality-fix.md`. Keep non-root
  user + `gh` (already solved).
- **`k8s/cronjob-quality.yaml`** — a *separate* CronJob `glm-loop-quality`
  (leave the existing build-guard `glm-loop` as-is), env:
  - `GIT_BRANCH=quality`, `LOOP_PR_BASE=quality`, `LOOP_BRANCH_PREFIX=glm_quality`
  - `LOOP_GATE_CMD=bash .glm-loop/gate.sh`
  - `LOOP_SKILL_FILE=skills/quality-fix.md`
  - `WARN_STEP=10`
  - `LOOP_MODEL=glm-5.2:cloud`, `OLLAMA_HOST=http://host.docker.internal:11434`
  - `GH_TOKEN` from Secret `glm-loop-git`
  - schedule: **a few times a week** (e.g. `0 12 * * 1,3,5` Asia/Riyadh) to avoid PR overload.

### Known follow-up (deferred issue from build-guard)

PRs currently also contain the loop's own seeded files (`loop.sh`, `skills/`,
`STATE.md`, `logs/`). Before this loop is "for real," fix the entrypoint to add
those to `.git/info/exclude` after clone so PRs contain **only** the code fixes.
(User chose to defer this on the build-guard; it matters more here since these
PRs are meant to be reviewed and merged regularly.)

## Files to create / modify

- New in target repo (seed on `quality`): `Directory.Build.props`,
  `.editorconfig`, `.glm-loop/gate.sh`
- New loop assets: `skills/quality-fix.md`, `k8s/cronjob-quality.yaml`
- Modify: entrypoint (baseline computation) -> rebuild image
- Reuse unchanged: `loop-A.sh`, Secret, minikube infra, Ollama networking

## Verification (end-to-end, before scheduling)

1. Seed the `quality` branch; confirm `dotnet build` succeeds and note the
   warning count (the initial baseline).
2. Build the image; run `debug-claude-job` to confirm GLM reachable (non-root).
3. Run `glm-loop-quality` as a **manual Job** once. Confirm the pod log shows
   `warnings=<W0> target=<W0-STEP>`, GLM fixes a batch, gate goes green.
4. Inspect the resulting PR into `quality`: it should reduce the warning count by
   >= STEP, touch **no** Migrations/auth files, and contain only sensible
   annotation/guard changes.
5. Merge it, re-run the Job, and confirm the new baseline is lower (ratchet works).
6. Only then enable the CronJob schedule.

## Rollout guidance

- Start with **nullable only** (or `latest-recommended`) and `WARN_STEP=10`;
  widen analyzer scope after the count is under control.
- Review the first several PRs closely — every bad fix becomes a new rule in
  `skills/quality-fix.md` (harden the skill, as the README's build-order advises).
- When `quality` reaches zero warnings for a rule set, merge `quality` -> `dev`,
  then widen the rules (`latest-all`, or flip `TreatWarningsAsErrors=true` on the
  now-clean set to keep it clean going forward).

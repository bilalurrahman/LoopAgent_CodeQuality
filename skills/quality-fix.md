# Skill: quality-fix

You are fixing Roslyn analyzer warnings (CA*/IDE*) on a .NET solution as part of
an automated quality-ratchet loop. Your job each run is to reduce the warning
count to the target with **small, correct, mechanical** changes, then let the
loop open a PR.

## Do
- Make the **smallest correct change** that removes the warning:
  - `CA1305 / CA1304 / CA1311` — pass a culture to string ops. Use
    `CultureInfo.InvariantCulture` for machine/round-trip strings, or
    `ToLowerInvariant()` / `ToUpperInvariant()` where an invariant case
    conversion is intended. Use `CultureInfo.CurrentCulture` only for
    genuinely user-facing text.
  - `CA1860` — replace `collection.Any()` with `collection.Count > 0`
    (or `.Length > 0`), and `!collection.Any()` with `.Count == 0`, using the
    member that actually exists on the type.
  - `CA1822` — add `static` to instance methods that don't touch instance state.
  - `CA1805` — remove redundant initialization to `default(T)`.
  - `IDE0005` — remove unused `using` directives.
  - `IDE0044` — mark fields `readonly` when only assigned in the constructor.
  - `IDE0051 / IDE0052 / IDE0060` — remove dead private members / unused
    parameters **only when clearly unused** across the whole project.
- Prefer fixing a whole file cleanly over scattering partial fixes.
- Rebuild after each file; keep a change only if the warning count strictly
  drops and the build still succeeds.

## Never
- Suppress a warning with `!` (null-forgiving), `#pragma warning disable`,
  `[SuppressMessage]`, or by lowering severity in `.editorconfig`.
- Change public API signatures, method names, or runtime behaviour to silence a
  warning.
- Touch generated or sensitive code: `**/Migrations/**`, auth / identity, EF
  model configuration, or anything named `*.Job`.

## Escalate (skip, don't guess)
If a warning's correct fix is ambiguous or needs a design/behavioural decision
(e.g. many `CA1848` LoggerMessage conversions, `CA1873` guard rewrites), **skip
it** this run rather than risk an incorrect change. It will remain for a human or
a later, better-scoped run. Note skipped rules in `STATE.md` if present.

## Definition of done for a run
The gate is green when the working tree has at least `WARN_STEP` fewer unique
analyzer warnings than the freshly-cloned base branch. Stop as soon as it is
green — do not over-fix; smaller PRs review faster.

"""Quality-ratchet loop agent.

Stateless, baseline-relative warning ratchet:

    clone/prepare base branch  ->  build, count warnings = W0  (baseline)
    target = max(0, W0 - STEP)
    fix loop: ask the model to fix warnings until count <= target
    commit + push work branch  ->  open PR into the base branch

The base branch's current warning count IS the state; each merged PR lowers it,
so the next run's target tightens automatically until it reaches zero.

Every step is emitted through the EventBus, which both writes a JSONL run log
and streams to the live dashboard (see server.py / ui/index.html).

Run:
    python loop_agent.py --repo <path> [--simulate] [--no-serve] [--open-pr]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from events import EventBus
import analyzer as wmod
import llm as llmmod
import gitops
import server as srv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Rules we trust the model to fix mechanically, in priority order.
FIXABLE_PRIORITY = ["CA1860", "CA1305", "CA1304", "CA1311", "CA1822", "CA1805", "CA1000"]


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ══════════════════════════════════════════════════════════════════════════
#  Real loop
# ══════════════════════════════════════════════════════════════════════════
def run_real(cfg, bus: EventBus):
    repo = cfg["repo"]
    sln = cfg["sln"]
    step = cfg["step"]
    run_id = cfg["run_id"]
    work_branch = f"{cfg['branch_prefix']}/{run_id}"

    bus.emit("run_start", run_id=run_id, repo=repo, model=cfg["model"],
             base_branch=cfg["base_branch"], step=step)

    # ── prepare ───────────────────────────────────────────────────────────
    bus.emit("phase", name="prepare")
    # Source dir = the folder containing the .sln/.csproj (e.g. backend/ or server/).
    src_dir = os.path.dirname(sln) or "."
    _ensure_seed_files(repo, bus, src_dir)
    gitops.ensure_branch(repo, work_branch)
    bus.log(f"working on branch {work_branch}")

    # ── baseline ──────────────────────────────────────────────────────────
    bus.emit("phase", name="baseline")
    bus.log("building baseline…")
    base = wmod.build_and_parse(sln, repo, incremental=False)
    if not base.ok:
        bus.emit("error", msg="baseline build has errors", errors=base.errors[:10])
        bus.emit("run_end", status="error")
        return
    w0 = base.count
    target = max(0, w0 - step)
    bus.emit("baseline", w0=w0, target=target, per_rule=base.per_rule,
             warnings=base.warnings_dicts())
    bus.log(f"baseline W0={w0}  target={target}  ({step} to fix)")

    # ── fix loop ──────────────────────────────────────────────────────────
    bus.emit("phase", name="fix-loop")
    client = llmmod.OllamaClient(cfg["ollama_host"], cfg["model"])
    current = base
    for it in range(1, cfg["max_iters"] + 1):
        if current.count <= target:
            break
        bus.emit("iteration_start", n=it)
        batch_files = _select_batch(current.warnings, cfg["batch_files"])
        flat = [w for group in batch_files.values() for w in group]
        bus.emit("select", warnings=[_wd(w) for w in flat])
        bus.log(f"iteration {it}: {len(flat)} warnings across {len(batch_files)} file(s)")

        for rel, group in batch_files.items():
            if current.count <= target:
                break
            current = _fix_one_file(repo, sln, rel, group, client, current, bus)

        bus.emit("iteration_end", n=it, count=current.count)

    # ── gate ──────────────────────────────────────────────────────────────
    bus.emit("phase", name="gate")
    green = current.count <= target
    bus.emit("gate", count=current.count, target=target, green=green)

    # ── publish ───────────────────────────────────────────────────────────
    # Only publish when the run made real progress. Uncommitted seed files on
    # their own are not a fix and must not become a PR.
    if current.count < w0 and gitops.has_changes(repo):
        bus.emit("phase", name="publish")
        gitops.stage_all(repo)
        msg = _commit_message(w0, current.count, base.per_rule, current.per_rule)
        sha = gitops.commit(repo, msg, cfg["author_name"], cfg["author_email"])
        bus.emit("commit", sha=sha[:10], message=msg.splitlines()[0])
        bus.log(f"committed {sha[:10]}")
        if cfg["open_pr"]:
            try:
                gitops.push(repo, work_branch)
                num, url = gitops.open_pr(repo, cfg["pr_base"], work_branch,
                                          _commit_message(w0, current.count, base.per_rule, current.per_rule).splitlines()[0],
                                          _pr_body(w0, current.count, base.per_rule, current.per_rule))
                if url:
                    bus.emit("pr", url=url, number=num)
                    bus.log(f"opened PR {url}")
                else:
                    bus.log("gh unavailable — pushed branch, open PR manually", level="warn")
            except Exception as e:  # noqa: BLE001
                bus.log(f"publish failed: {e}", level="error")
    else:
        bus.log("no warnings fixed this run — nothing to publish", level="warn")

    status = "green" if green else "escalated"
    bus.emit("run_end", status=status, start_count=w0, end_count=current.count,
             fixed=w0 - current.count)
    bus.log(f"run complete: {w0} -> {current.count} ({status})",
            level="info" if green else "warn")


def _fix_one_file(repo, sln, rel, group, client, current, bus):
    abspath = os.path.join(repo, rel)
    try:
        source = open(abspath, encoding="utf-8").read()
    except OSError as e:
        bus.log(f"cannot read {rel}: {e}", level="error")
        return current
    bus.emit("llm_call", file=rel, model=client.model, warnings=len(group))
    try:
        raw = client.chat(llmmod.SYSTEM_PROMPT, llmmod.build_user_prompt(rel, source, [_wd(w) for w in group]))
    except llmmod.LLMError as e:
        bus.log(f"LLM error on {rel}: {e}", level="error")
        return current
    edits = llmmod.extract_edits(raw)
    if not edits:
        bus.log(f"no edits returned for {rel}", level="warn")
        return current

    new_text, applied = source, []
    for e in edits:
        new_text, ok, note = llmmod.apply_edit_to_text(new_text, e)
        if ok:
            applied.append(e)
        else:
            bus.log(f"skip edit ({e.get('code')}): {note}", level="warn")
    if not applied:
        return current

    open(abspath, "w", encoding="utf-8").write(new_text)
    # Full rebuild: an incremental build would only recompile the touched
    # project, so warnings from untouched projects would vanish from the output
    # and the count would look artificially low. Correctness over speed here.
    rebuilt = wmod.build_and_parse(sln, repo, incremental=False)
    # Keep only if the tree still builds AND warnings strictly decreased.
    if rebuilt.ok and rebuilt.count < current.count:
        for e in applied:
            bus.emit("edit_applied", file=rel, code=e.get("code"), reason=e.get("reason"))
        bus.emit("rebuild", count=rebuilt.count, per_rule=rebuilt.per_rule,
                 warnings=rebuilt.warnings_dicts())
        bus.log(f"{rel}: {current.count} -> {rebuilt.count} (kept {len(applied)} edit(s))")
        return rebuilt
    # Roll back — the change didn't help or broke the build.
    gitops.checkout_file(repo, rel)
    reason = "build broke" if not rebuilt.ok else "no improvement"
    bus.log(f"{rel}: reverted ({reason})", level="warn")
    return current


# ══════════════════════════════════════════════════════════════════════════
#  Simulate loop — drives the UI from the real captured warning list,
#  no LLM or git side effects. Realistic timing.
# ══════════════════════════════════════════════════════════════════════════
def run_simulate(cfg, bus: EventBus, base_count=None, warnings_list=None):
    run_id = cfg["run_id"]
    step = cfg["step"]
    speed = cfg["speed"]

    if warnings_list is None:
        wl_path = os.path.join(HERE, "warnings_baseline.json")
        warnings_list = json.load(open(wl_path, encoding="utf-8"))
    remaining = list(warnings_list)
    if base_count is not None:
        remaining = remaining[:base_count]

    def sleep(x):
        time.sleep(x / speed)

    def per_rule(ws):
        return dict(sorted(collections.Counter(w["code"] for w in ws).items(), key=lambda kv: -kv[1]))

    bus.emit("run_start", run_id=run_id, repo=cfg.get("repo") or cfg.get("repo_name"),
             model=cfg["model"] + " (sim)", base_branch=cfg["base_branch"], step=step)
    bus.emit("phase", name="prepare")
    bus.log("cloning base branch…"); sleep(0.6)
    bus.log("ensuring seed files (.editorconfig, Directory.Build.props)…"); sleep(0.5)

    bus.emit("phase", name="baseline")
    bus.log("building baseline (dotnet build)…"); sleep(1.2)
    w0 = len(remaining)
    target = max(0, w0 - step)
    bus.emit("baseline", w0=w0, target=target, per_rule=per_rule(remaining),
             warnings=list(remaining))
    bus.log(f"baseline W0={w0}  target={target}")

    bus.emit("phase", name="fix-loop")
    it = 0
    reasons = {
        "CA1305": "pass CultureInfo.InvariantCulture to string.Format",
        "CA1304": "pass CultureInfo to ToLower/ToUpper",
        "CA1311": "use ToLowerInvariant()/ToUpperInvariant()",
        "CA1860": "replace .Any() with .Count > 0",
        "CA1822": "mark method static",
        "CA1805": "remove redundant default initialization",
        "CA1848": "use LoggerMessage delegate",
        "CA1873": "guard expensive log arg with IsEnabled",
        "CA1869": "cache JsonSerializerOptions instance",
        "CA1000": "do not declare static members on generic types",
        "SYSLIB1045": "convert to GeneratedRegex",
    }
    while len(remaining) > target:
        it += 1
        bus.emit("iteration_start", n=it)
        # A batch: a few warnings grouped by file, biased to fixable rules.
        remaining.sort(key=lambda w: (FIXABLE_PRIORITY.index(w["code"]) if w["code"] in FIXABLE_PRIORITY else 99))
        take = min(cfg["batch_files"] * 2, len(remaining) - target)
        take = max(1, take)
        batch = remaining[:take]
        by_file = collections.OrderedDict()
        for w in batch:
            by_file.setdefault(w["file"], []).append(w)
        bus.emit("select", warnings=list(batch))
        bus.log(f"iteration {it}: fixing {len(batch)} warning(s) in {len(by_file)} file(s)")
        sleep(0.5)
        for rel, group in by_file.items():
            if len(remaining) <= target:
                break
            bus.emit("llm_call", file=rel, model=cfg["model"] + " (sim)", warnings=len(group))
            sleep(0.9)
            for w in group:
                if len(remaining) <= target:
                    break
                remaining.remove(w)
                bus.emit("edit_applied", file=rel, code=w["code"],
                         reason=reasons.get(w["code"], "apply minimal fix"))
                sleep(0.25)
            bus.emit("rebuild", count=len(remaining), per_rule=per_rule(remaining),
                     warnings=list(remaining))
            bus.log(f"{os.path.basename(rel)}: rebuilt, {len(remaining)} warnings left")
            sleep(0.4)
        bus.emit("iteration_end", n=it, count=len(remaining))

    bus.emit("phase", name="gate")
    green = len(remaining) <= target
    bus.emit("gate", count=len(remaining), target=target, green=green)
    sleep(0.4)

    bus.emit("phase", name="publish")
    fixed = w0 - len(remaining)
    bus.log("staging changes & committing…"); sleep(0.6)
    sha = uuid.uuid4().hex[:10]
    bus.emit("commit", sha=sha, message=f"quality: fix {fixed} analyzer warnings")
    bus.log("pushing branch & opening PR…"); sleep(0.6)
    bus.emit("pr", number=100 + it, url=f"https://github.com/bilalurrahman/HattanMedicalHistory/pull/{100+it}")
    bus.emit("run_end", status="green", start_count=w0, end_count=len(remaining), fixed=fixed)
    bus.log(f"run complete: {w0} -> {len(remaining)} (green)")
    return len(remaining), remaining


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════
def _wd(w):
    return w if isinstance(w, dict) else {"file": w.file, "line": w.line, "col": w.col, "code": w.code, "message": w.message}


def _select_batch(warnings, max_files):
    def prio(w):
        return FIXABLE_PRIORITY.index(w.code) if w.code in FIXABLE_PRIORITY else 99
    ordered = sorted(warnings, key=prio)
    by_file = collections.OrderedDict()
    for w in ordered:
        if w.code not in FIXABLE_PRIORITY:
            continue
        by_file.setdefault(w.file, []).append(w)
        if len(by_file) >= max_files:
            break
    if not by_file:  # fall back to any file
        for w in ordered:
            by_file.setdefault(w.file, []).append(w)
            if len(by_file) >= max_files:
                break
    return by_file


def _ensure_seed_files(repo, bus, src_dir="backend"):
    pairs = [
        (os.path.join(ROOT, "seed", "Directory.Build.props"), os.path.join(repo, src_dir, "Directory.Build.props")),
        (os.path.join(ROOT, "seed", ".editorconfig"), os.path.join(repo, src_dir, ".editorconfig")),
        (os.path.join(ROOT, "seed", ".glm-loop", "gate.sh"), os.path.join(repo, ".glm-loop", "gate.sh")),
    ]
    for src, dst in pairs:
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            bus.log(f"seeded {os.path.relpath(dst, repo)}")


def _commit_message(w0, wn, pr0, prn):
    fixed = w0 - wn
    return f"quality: fix {fixed} analyzer warnings ({w0} -> {wn})"


def _pr_body(w0, wn, pr0, prn):
    lines = [
        "Automated quality-ratchet run.",
        "",
        f"- Warnings: **{w0} -> {wn}** ({w0 - wn} fixed)",
        "- Fixes are minimal, mechanical CA*/IDE* corrections — no suppression, no API/behaviour changes.",
        "- Migrations / auth / EF config untouched.",
        "",
        "| rule | before | after |",
        "| --- | ---: | ---: |",
    ]
    for code in sorted(set(pr0) | set(prn)):
        lines.append(f"| {code} | {pr0.get(code,0)} | {prn.get(code,0)} |")
    return "\n".join(lines)


def build_config(args):
    return {
        "repo": args.repo or env("REPO_PATH"),
        "sln": args.sln or env("LOOP_SLN", "backend/HattanHealthTracker.sln"),
        "base_branch": args.base_branch or env("GIT_BRANCH", "quality"),
        "pr_base": args.pr_base or env("LOOP_PR_BASE", "quality"),
        "branch_prefix": args.branch_prefix or env("LOOP_BRANCH_PREFIX", "glm_quality"),
        "step": int(args.step or env("WARN_STEP", "10")),
        "max_iters": int(args.max_iters or env("MAX_ITERS", "5")),
        "batch_files": int(args.batch_files or env("BATCH_FILES", "3")),
        "model": args.model or env("LOOP_MODEL", llmmod.DEFAULT_MODEL),
        "ollama_host": args.ollama_host or env("OLLAMA_HOST", llmmod.DEFAULT_HOST),
        "open_pr": args.open_pr,
        "author_name": env("GIT_AUTHOR_NAME", "quality-loop[bot]"),
        "author_email": env("GIT_AUTHOR_EMAIL", "quality-loop@users.noreply.github.com"),
        "speed": float(args.speed or 1.0),
        "run_id": time.strftime("%Y%m%d-%H%M%S"),
    }


def main():
    ap = argparse.ArgumentParser(description="Quality-ratchet loop agent")
    ap.add_argument("--repo", help="path to target repo working tree")
    ap.add_argument("--sln")
    ap.add_argument("--base-branch")
    ap.add_argument("--pr-base")
    ap.add_argument("--branch-prefix")
    ap.add_argument("--step", type=int)
    ap.add_argument("--max-iters", type=int)
    ap.add_argument("--batch-files", type=int)
    ap.add_argument("--model")
    ap.add_argument("--ollama-host")
    ap.add_argument("--open-pr", action="store_true", help="push + open a PR (real mode)")
    ap.add_argument("--simulate", action="store_true", help="drive the UI from captured warnings, no LLM/git")
    ap.add_argument("--sim-runs", type=int, default=1, help="simulate N sequential ratchet runs")
    ap.add_argument("--speed", type=float, help="simulate speed multiplier (higher=faster)")
    ap.add_argument("--daemon", action="store_true",
                    help="serve the dashboard + scheduler and wait for runs triggered from the UI")
    ap.add_argument("--no-serve", action="store_true", help="do not start the dashboard server")
    ap.add_argument("--port", type=int, default=int(env("UI_PORT", "8787")))
    ap.add_argument("--host", default=env("UI_HOST", "127.0.0.1"))
    ap.add_argument("--keep-alive", action="store_true", help="keep server running after the run ends")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    cfg = build_config(args)
    run_dir = os.path.join(ROOT, "runs", cfg["run_id"])
    bus = EventBus(jsonl_path=os.path.join(run_dir, "events.jsonl"))

    # ── daemon: serve dashboard + scheduler, wait for UI-triggered runs ────
    if args.daemon:
        import control
        controller = control.Controller(bus)
        srv.start_server(bus, args.host, args.port, controller=controller)
        print("\n  +-- Quality-Loop control panel -----------------------------")
        print(f"  |   open  http://{args.host}:{args.port}")
        print(f"  |   repos: {len(controller.repos)}   schedules: {len(controller.schedules)}")
        print("  |   pick a repo, then Run now or Schedule.")
        print("  +-----------------------------------------------------------\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    httpd = None
    if not args.no_serve:
        httpd = srv.start_server(bus, args.host, args.port)
        print("\n  +-- Quality-Loop dashboard ---------------------------------")
        print(f"  |   open  http://{args.host}:{args.port}")
        print("  +-----------------------------------------------------------\n")
        time.sleep(1.0)  # give the browser a moment if opened

    try:
        if args.simulate:
            base_count, wl = None, None
            for r in range(args.sim_runs):
                cfg["run_id"] = time.strftime("%Y%m%d-%H%M%S") + f"-{r+1}"
                base_count, wl = run_simulate(cfg, bus, base_count=base_count, warnings_list=wl)
                if base_count <= 0:
                    bus.log("reached zero warnings — ratchet complete 🎉")
                    break
                if r < args.sim_runs - 1:
                    time.sleep(1.5)
        else:
            if not cfg["repo"]:
                print("error: --repo (or REPO_PATH) is required in real mode", file=sys.stderr)
                sys.exit(2)
            run_real(cfg, bus)
    except KeyboardInterrupt:
        pass

    if httpd and (args.keep_alive or not args.no_serve):
        print("  dashboard still live — Ctrl-C to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

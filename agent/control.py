"""Control layer: repo registry + Runner + in-process Scheduler.

This turns the dashboard from a passive monitor into a control panel:
  * a Registry of known repos (config/repos.json),
  * a Runner that executes ONE run at a time in a worker thread (cloning the
    target repo first if it isn't on disk yet), sharing the live EventBus,
  * a Scheduler thread that fires runs on an interval or at a daily time
    (config/schedules.json).

Note: this scheduler only fires while THIS process is alive. For always-on,
unattended scheduling use the Kubernetes CronJob (k8s/cronjob-quality.yaml) —
that's the durable path. This is the convenient local one.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

from events import EventBus

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG_DIR = os.path.join(ROOT, "config")
REPOS_FILE = os.path.join(CONFIG_DIR, "repos.json")
SCHED_FILE = os.path.join(CONFIG_DIR, "schedules.json")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _save(path, data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class Controller:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._lock = threading.Lock()
        self._running = False
        self._current_run_id = None
        self._run_counter = 0
        self.repos = _load(REPOS_FILE, [])
        self.schedules = _load(SCHED_FILE, [])
        for s in self.schedules:
            s.setdefault("next_run", self._compute_next(s))
        self._stop = threading.Event()
        self._sched_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._sched_thread.start()

    # ── introspection ─────────────────────────────────────────────────────
    def is_running(self) -> bool:
        return self._running

    def config_snapshot(self) -> dict:
        return {
            "repos": self.repos,
            "schedules": self.schedules,
            "running": self._running,
            "current_run_id": self._current_run_id,
            "defaults": {
                "model": _env("LOOP_MODEL", "glm-5.2:cloud"),
                "step": int(_env("WARN_STEP", "10")),
            },
        }

    def get_repo(self, key):
        return next((r for r in self.repos if r.get("key") == key), None)

    def _resolve_repo(self, params):
        """Return a repo dict from either a saved key or an inline custom spec."""
        cust = params.get("custom") or {}
        if params.get("repo_key") == "custom" or cust.get("url"):
            url = (cust.get("url") or "").strip()
            if not url:
                return None
            name = url.rstrip("/").split("/")[-1].replace(".git", "") or "repo"
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "repo"
            branch = (cust.get("branch") or "main").strip()
            return {
                "key": "custom",
                "name": name,
                "url": url,
                "path": os.path.join(tempfile.gettempdir(), "qualityloop", slug),
                "sln": (cust.get("sln") or "").strip(),
                "base_branch": branch,
                "pr_base": (cust.get("pr_base") or branch).strip(),
                "branch_prefix": "glm_quality",
            }
        return self.get_repo(params.get("repo_key"))

    # ── run ───────────────────────────────────────────────────────────────
    def start_run(self, params: dict) -> tuple[bool, str]:
        """params: {repo_key, mode: real|simulate, open_pr, step}"""
        with self._lock:
            if self._running:
                return False, "a run is already in progress"
            repo = self._resolve_repo(params)
            is_sim = params.get("mode") == "simulate"
            if repo is None and not is_sim:
                return False, "no repo selected (pick one or paste a custom URL)"
            if repo is not None and not is_sim and not repo.get("sln"):
                return False, "custom repo needs a project/solution path (e.g. server/server.csproj)"
            self._run_counter += 1
            run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{self._run_counter}"
            self._running = True
            self._current_run_id = run_id
        cfg = self._build_cfg(repo, params, run_id)
        t = threading.Thread(target=self._run_thread, args=(cfg, params.get("mode", "simulate")), daemon=True)
        t.start()
        return True, run_id

    def _build_cfg(self, repo, params, run_id) -> dict:
        repo = repo or {}
        return {
            "run_id": run_id,
            "repo": repo.get("path"),
            "repo_url": repo.get("url"),
            "repo_name": repo.get("name") or repo.get("key") or "simulated",
            "sln": repo.get("sln", "backend/HattanHealthTracker.sln"),
            "base_branch": repo.get("base_branch", "quality"),
            "pr_base": repo.get("pr_base", repo.get("base_branch", "quality")),
            "branch_prefix": repo.get("branch_prefix", "glm_quality"),
            "step": int(params.get("step") or _env("WARN_STEP", "10")),
            "max_iters": int(_env("MAX_ITERS", "5")),
            "batch_files": int(_env("BATCH_FILES", "3")),
            "model": _env("LOOP_MODEL", "glm-5.2:cloud"),
            "ollama_host": _env("OLLAMA_HOST", "http://localhost:11434"),
            "open_pr": bool(params.get("open_pr")),
            "author_name": _env("GIT_AUTHOR_NAME", "quality-loop[bot]"),
            "author_email": _env("GIT_AUTHOR_EMAIL", "quality-loop@users.noreply.github.com"),
            "speed": float(params.get("speed") or 1.5),
        }

    def _run_thread(self, cfg, mode):
        import loop_agent  # late import to avoid an import cycle
        run_dir = os.path.join(ROOT, "runs", cfg["run_id"])
        self.bus.reset(jsonl_path=os.path.join(run_dir, "events.jsonl"))
        try:
            if mode == "simulate":
                loop_agent.run_simulate(cfg, self.bus)
            else:
                self._ensure_clone(cfg)
                loop_agent.run_real(cfg, self.bus)
        except Exception as e:  # noqa: BLE001
            self.bus.emit("error", msg=f"run failed: {e}")
            self.bus.emit("run_end", status="error")
            self.bus.log(f"run failed: {e}", level="error")
        finally:
            with self._lock:
                self._running = False
                self._current_run_id = None

    def _ensure_clone(self, cfg):
        repo, url, branch = cfg.get("repo"), cfg.get("repo_url"), cfg.get("base_branch")
        if repo and os.path.isdir(os.path.join(repo, ".git")):
            return
        if not url:
            raise RuntimeError("repo path missing and no clone url configured")
        self.bus.log(f"cloning {url} ({branch})…")
        os.makedirs(os.path.dirname(repo), exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", branch, "--depth", "50", url, repo],
            check=True, capture_output=True, text=True,
        )

    # ── schedules ─────────────────────────────────────────────────────────
    def add_schedule(self, spec: dict) -> dict:
        s = {
            "id": uuid.uuid4().hex[:8],
            "repo_key": spec.get("repo_key"),
            "custom": spec.get("custom") or None,
            "mode": spec.get("mode", "simulate"),
            "open_pr": bool(spec.get("open_pr")),
            "step": int(spec.get("step") or 10),
            "type": spec.get("type", "interval"),        # interval | daily
            "every_minutes": int(spec.get("every_minutes") or 60),
            "at": spec.get("at", "12:00"),               # HH:MM local
            "days": spec.get("days") or [],              # weekday ints, Mon=0; [] = every day
            "enabled": bool(spec.get("enabled", True)),
            "last_run": None,
        }
        s["next_run"] = self._compute_next(s)
        with self._lock:
            self.schedules.append(s)
            _save(SCHED_FILE, self.schedules)
        return s

    def delete_schedule(self, sid: str) -> bool:
        with self._lock:
            n = len(self.schedules)
            self.schedules = [s for s in self.schedules if s.get("id") != sid]
            _save(SCHED_FILE, self.schedules)
            return len(self.schedules) < n

    def toggle_schedule(self, sid: str, enabled: bool) -> bool:
        with self._lock:
            for s in self.schedules:
                if s.get("id") == sid:
                    s["enabled"] = enabled
                    s["next_run"] = self._compute_next(s)
                    _save(SCHED_FILE, self.schedules)
                    return True
        return False

    def _compute_next(self, s, after=None) -> float:
        now = after or time.time()
        if s.get("type") == "interval":
            return now + max(1, int(s.get("every_minutes", 60))) * 60
        # daily at HH:MM, optionally restricted to weekdays
        try:
            hh, mm = (int(x) for x in str(s.get("at", "12:00")).split(":"))
        except ValueError:
            hh, mm = 12, 0
        days = s.get("days") or list(range(7))
        base = datetime.datetime.fromtimestamp(now)
        for add in range(0, 8):
            cand = (base + datetime.timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand.timestamp() > now and cand.weekday() in days:
                return cand.timestamp()
        return now + 86400

    def _scheduler_loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(20)

    def _tick(self):
        now = time.time()
        due = None
        with self._lock:
            for s in self.schedules:
                if s.get("enabled") and s.get("next_run", 0) <= now:
                    due = s
                    break
        if not due:
            return
        if self._running:
            return  # try again next tick
        ok, msg = self.start_run(
            {"repo_key": due["repo_key"], "custom": due.get("custom"), "mode": due["mode"],
             "open_pr": due["open_pr"], "step": due["step"]}
        )
        with self._lock:
            due["last_run"] = now
            due["next_run"] = self._compute_next(due, after=now)
            _save(SCHED_FILE, self.schedules)
        self.bus.log(f"scheduler fired '{due['id']}' ({due['repo_key']}) -> {msg}",
                     level="info" if ok else "warn")

"""Event bus for the quality-ratchet loop.

Every meaningful step the loop takes is emitted here as a structured event.
The bus does three things at once:

  1. Appends each event to a JSONL file (durable run record).
  2. Maintains an in-memory *state snapshot* so a browser that connects late
     still gets the full current picture before live updates start.
  3. Fans events out to any number of live subscribers (the SSE endpoint),
     each backed by its own thread-safe queue.

Stdlib only. Thread-safe.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Callable


def now_ms() -> int:
    return int(time.time() * 1000)


class EventBus:
    def __init__(self, jsonl_path: str | None = None):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._seq = 0
        self._jsonl_path = jsonl_path
        if jsonl_path:
            os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)
            # Truncate at start of a run.
            open(jsonl_path, "w", encoding="utf-8").close()

        # Live snapshot the UI rebuilds itself from on (re)connect.
        self.state: dict[str, Any] = {
            "run_id": None,
            "status": "idle",           # idle | running | green | escalated | error
            "repo": None,
            "model": None,
            "base_branch": None,
            "step": None,
            "phase": None,              # prepare | baseline | fix-loop | gate | publish | done
            "baseline": None,           # W0
            "target": None,
            "current": None,            # current warning count
            "per_rule": {},             # {code: count} current
            "per_rule_baseline": {},
            "iterations": [],           # [{n, start, batch:[...], edits:[...], count, delta}]
            "iteration": 0,
            "warnings": [],             # current outstanding warning list
            "edits": [],                # recent applied edits (rolling)
            "logs": [],                 # rolling log lines
            "pr": None,
            "commit": None,
            "started_at": None,
            "ended_at": None,
            "fixed": 0,
        }

    # ── emit ──────────────────────────────────────────────────────────────
    def emit(self, etype: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            evt = {"seq": self._seq, "ts": now_ms(), "type": etype, **data}
            self._apply_to_state(evt)
            if self._jsonl_path:
                with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(evt) + "\n")
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(evt)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
        return evt

    def log(self, msg: str, level: str = "info") -> None:
        self.emit("log", level=level, msg=msg)

    # ── subscribe (for SSE) ───────────────────────────────────────────────
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.state))

    # ── state reducer ─────────────────────────────────────────────────────
    def _apply_to_state(self, evt: dict) -> None:
        s = self.state
        t = evt["type"]
        if t == "run_start":
            s.update(
                run_id=evt.get("run_id"),
                status="running",
                repo=evt.get("repo"),
                model=evt.get("model"),
                base_branch=evt.get("base_branch"),
                step=evt.get("step"),
                started_at=evt["ts"],
                phase="prepare",
            )
        elif t == "phase":
            s["phase"] = evt.get("name")
        elif t == "baseline":
            s["baseline"] = evt.get("w0")
            s["current"] = evt.get("w0")
            s["target"] = evt.get("target")
            s["per_rule"] = dict(evt.get("per_rule", {}))
            s["per_rule_baseline"] = dict(evt.get("per_rule", {}))
            s["warnings"] = evt.get("warnings", [])
        elif t == "iteration_start":
            s["iteration"] = evt.get("n")
            s["iterations"].append(
                {"n": evt.get("n"), "start": evt["ts"], "batch": [], "edits": [], "count": s.get("current"), "delta": 0}
            )
        elif t == "select":
            if s["iterations"]:
                s["iterations"][-1]["batch"] = evt.get("warnings", [])
        elif t == "edit_applied":
            rec = {"file": evt.get("file"), "code": evt.get("code"), "reason": evt.get("reason"), "ts": evt["ts"]}
            s["edits"] = ([rec] + s["edits"])[:30]
            if s["iterations"]:
                s["iterations"][-1]["edits"].append(rec)
        elif t == "rebuild":
            prev = s.get("current")
            s["current"] = evt.get("count")
            s["per_rule"] = dict(evt.get("per_rule", {}))
            s["warnings"] = evt.get("warnings", s["warnings"])
            if s["iterations"]:
                s["iterations"][-1]["count"] = evt.get("count")
                if prev is not None and evt.get("count") is not None:
                    s["iterations"][-1]["delta"] = evt.get("count") - prev
            if s.get("baseline") is not None and s.get("current") is not None:
                s["fixed"] = max(0, s["baseline"] - s["current"])
        elif t == "gate":
            s["current"] = evt.get("count", s["current"])
            if evt.get("green"):
                s["status"] = "green"
        elif t == "commit":
            s["commit"] = {"sha": evt.get("sha"), "message": evt.get("message")}
        elif t == "pr":
            s["pr"] = {"url": evt.get("url"), "number": evt.get("number")}
        elif t == "run_end":
            s["status"] = evt.get("status", s["status"])
            s["ended_at"] = evt["ts"]
            s["phase"] = "done"
            if s.get("baseline") is not None and s.get("current") is not None:
                s["fixed"] = max(0, s["baseline"] - s["current"])
        elif t == "error":
            s["status"] = "error"
        elif t == "log":
            s["logs"] = ([{"ts": evt["ts"], "level": evt.get("level"), "msg": evt.get("msg")}] + s["logs"])[:200]

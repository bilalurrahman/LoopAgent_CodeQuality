"""LLM client + edit protocol for the quality loop.

Talks to Ollama's chat API (default model glm-5.2:cloud). Given a batch of
warnings that all live in ONE file, plus that file's numbered source, it asks
the model for a set of exact find/replace edits and parses them back out.

Design constraints baked into the prompt (mirror skills/quality-fix.md):
  * smallest correct change; real fixes, never suppression (`!`, #pragma) or
    severity changes;
  * never change public API signatures or runtime behaviour;
  * one file at a time, returned as strict JSON.
"""

from __future__ import annotations

import json
import re
import urllib.request

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "glm-5.2:cloud"

SYSTEM_PROMPT = """\
You are a meticulous .NET code-quality engineer. You fix Roslyn analyzer
warnings (CA*/IDE*) with the SMALLEST CORRECT change.

Hard rules:
- Make a real fix. NEVER suppress with `!` (null-forgiving), `#pragma warning
  disable`, [SuppressMessage], or by lowering severity.
- NEVER change public API signatures, method names, or runtime behaviour.
- Keep edits minimal and mechanical. Preserve surrounding formatting/indentation.
- Only touch the file you are given. Do not invent code you cannot see.
- If a warning's correct fix is ambiguous or needs a design decision, SKIP it
  (omit it from edits) rather than guessing.

Common fixes:
- CA1305/CA1304/CA1311: pass CultureInfo.InvariantCulture (or CurrentCulture for
  user-facing) to ToString/ToLower/ToUpper/string.Format/Parse. Prefer
  ToLowerInvariant()/ToUpperInvariant() where a culture-invariant lowercase is
  intended.
- CA1860: replace `.Any()` with `.Count > 0` / `.Length > 0` (or `.Count == 0`
  for `!.Any()`), choosing the member that exists on the type.
- CA1822: add `static` to an instance method that doesn't use instance state.
- CA1805: remove redundant initialization to default(T).
- CA1848/CA1873: leave for a human if the fix is non-trivial (SKIP).

Respond with STRICT JSON only, no prose, in this shape:
{"edits":[{"code":"CAxxxx","find":"<exact source substring>","replace":"<replacement>","reason":"<short>"}]}
The "find" string MUST appear verbatim in the file and be long enough to be
unique. If you cannot safely fix anything, return {"edits":[]}.
"""


class LLMError(Exception):
    pass


class OllamaClient:
    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL, timeout: int = 240):
        self.host = self._normalise_host(host)
        self.model = model
        self.timeout = timeout

    @staticmethod
    def _normalise_host(host: str) -> str:
        host = (host or DEFAULT_HOST).strip().rstrip("/")
        if "://" not in host:
            host = "http://" + host
        # 0.0.0.0 is a bind-all address for the server, not connectable by a
        # client — the OLLAMA_HOST env var is commonly set to it.
        host = host.replace("://0.0.0.0", "://127.0.0.1")
        return host

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Ollama request failed: {e}") from e
        return (body.get("message") or {}).get("content", "")


def build_user_prompt(rel_path: str, source: str, batch: list[dict]) -> str:
    numbered = "\n".join(f"{i+1:>4}: {ln}" for i, ln in enumerate(source.splitlines()))
    warn_lines = "\n".join(
        f"  - line {w['line']} col {w['col']} {w['code']}: {w['message']}" for w in batch
    )
    return (
        f"File: {rel_path}\n\n"
        f"Warnings to fix in this file:\n{warn_lines}\n\n"
        f"Source (with line numbers; do NOT include the numbers in your find/replace):\n"
        f"```csharp\n{numbered}\n```\n"
    )


def extract_edits(raw: str) -> list[dict]:
    """Pull the edits array out of a model response, tolerating code fences."""
    if not raw:
        return []
    # Strip ```json fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    # Fall back to the outermost {...}.
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    edits = obj.get("edits", []) if isinstance(obj, dict) else []
    clean = []
    for e in edits:
        if isinstance(e, dict) and e.get("find") and "replace" in e:
            clean.append(
                {
                    "code": e.get("code", ""),
                    "find": e["find"],
                    "replace": e["replace"],
                    "reason": e.get("reason", ""),
                }
            )
    return clean


# Forbidden patterns — reject any "fix" that is really a suppression.
_FORBIDDEN = (
    re.compile(r"#pragma\s+warning\s+disable"),
    re.compile(r"SuppressMessage"),
)


def apply_edit_to_text(text: str, edit: dict) -> tuple[str, bool, str]:
    """Apply one find/replace. Returns (new_text, applied, note)."""
    find, replace = edit["find"], edit["replace"]
    for rx in _FORBIDDEN:
        if rx.search(replace) and not rx.search(find):
            return text, False, "rejected: introduces suppression"
    n = text.count(find)
    if n == 0:
        return text, False, "find string not present"
    if n > 1:
        return text, False, f"find string ambiguous ({n} matches)"
    return text.replace(find, replace, 1), True, "ok"

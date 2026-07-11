"""Build the target solution and parse its analyzer warnings.

The canonical count is the number of UNIQUE `file(line,col): warning CODE`
diagnostics — stable across multi-project builds and restore chatter, and it
matches what a human reviewer sees. This mirrors seed/.glm-loop/gate.sh so the
Python loop and the shell gate always agree.
"""

from __future__ import annotations

import collections
import re
import subprocess
from dataclasses import dataclass, asdict

_WARN_RX = re.compile(
    r"([^\s(]+\.cs)\((\d+),(\d+)\): warning ([A-Z]+\d+): (.*?)(?: \[[^\]]*\])?$"
)
_ERR_RX = re.compile(r": error [A-Z]+\d+:")


@dataclass(frozen=True)
class Warning:
    file: str
    line: int
    col: int
    code: str
    message: str

    def key(self):
        return (self.file, self.line, self.col, self.code)


@dataclass
class BuildResult:
    ok: bool                 # build succeeded (no errors)
    warnings: list           # list[Warning], de-duplicated
    per_rule: dict           # {code: count}
    errors: list             # raw error lines
    raw_tail: str            # last lines of output for debugging

    @property
    def count(self) -> int:
        return len(self.warnings)

    def warnings_dicts(self) -> list[dict]:
        return [asdict(w) for w in self.warnings]


def _normalise_path(p: str) -> str:
    p = p.replace("\\", "/")
    idx = p.find("backend/")
    return p[idx:] if idx >= 0 else p


def parse_warnings(text: str) -> BuildResult:
    seen: "collections.OrderedDict[tuple, Warning]" = collections.OrderedDict()
    errors: list[str] = []
    for line in text.splitlines():
        if _ERR_RX.search(line):
            errors.append(line.strip())
        m = _WARN_RX.search(line.strip())
        if not m:
            continue
        f, ln, col, code, msg = m.groups()
        w = Warning(_normalise_path(f), int(ln), int(col), code, msg.strip())
        seen.setdefault(w.key(), w)
    warnings = list(seen.values())
    per_rule = dict(
        sorted(collections.Counter(w.code for w in warnings).items(), key=lambda kv: -kv[1])
    )
    tail = "\n".join(text.splitlines()[-15:])
    return BuildResult(ok=not errors, warnings=warnings, per_rule=per_rule, errors=errors, raw_tail=tail)


def build_and_parse(sln: str, cwd: str, incremental: bool = True) -> BuildResult:
    args = ["dotnet", "build", sln, "--nologo"]
    if not incremental:
        args += ["--no-incremental"]
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return parse_warnings(out)

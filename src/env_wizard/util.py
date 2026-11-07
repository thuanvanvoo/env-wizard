"""Shared helpers: paths, command execution, logging, JSON I/O, versions, locking."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------- paths

def plugin_root() -> Path:
    env = os.environ.get("ENV_WIZARD_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def home() -> Path:
    return Path(os.environ.get("ENV_WIZARD_USER_HOME") or Path.home())


class DataDirError(OSError):
    pass


def data_dir() -> Path:
    d = Path(os.environ.get("ENV_WIZARD_HOME") or home() / ".env-wizard")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DataDirError(f"env-wizard can't write to its folder {d} ({e.strerror}). "
                           "This can happen on managed computers; ask IT, or set ENV_WIZARD_HOME to a writable folder.")
    return d


def project_dir(explicit: Optional[str] = None) -> Path:
    return Path(explicit or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------- JSON

def read_json(path: Path, default=None):
    try:
        # utf-8-sig: Windows PowerShell 5.1 writes a BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # Keep a copy of the corrupt file instead of crashing or silently discarding it.
        bad = Path(str(path) + ".corrupt")
        shutil.copyfile(path, bad)
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- redaction

_SECRET_PATTERNS = [
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(sk-[A-Za-z0-9_\-]{20,})"),
    re.compile(r"(xox[abpr]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"(AKIA[0-9A-Z]{16})"),
    re.compile(r"(?i)((?:token|password|passwd|secret|api[_-]?key)\s*[=:]\s*)(\S+)"),
    re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        if pat.groups == 2:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        elif pat.pattern.startswith("(https?://)"):
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]@", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------- logging

class Log:
    def __init__(self, path: Optional[Path]):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, msg: str) -> None:
        if not self.path:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {redact(msg)}\n")


# ---------------------------------------------------------------- command execution

@dataclass
class Result:
    code: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out + "\n" + self.err).strip()


class Runner:
    """Runs commands. Every command and its output goes to the run log, never to the user."""

    def __init__(self, log: Optional[Log] = None, extra_env: Optional[Dict[str, str]] = None):
        self.log = log or Log(None)
        self.extra_env = dict(extra_env or {})

    def run(self, argv: Sequence[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
            timeout: int = 1800) -> Result:
        full_env = os.environ.copy()
        full_env.update(self.extra_env)
        if env:
            full_env.update(env)
        self.log.write(f"$ {' '.join(map(str, argv))}" + (f"   (in {cwd})" if cwd else ""))
        try:
            p = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None, env=full_env,
                               capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            res = Result(p.returncode, p.stdout or "", p.stderr or "")
        except FileNotFoundError as e:
            res = Result(127, "", f"command not found: {e}")
        except subprocess.TimeoutExpired:
            res = Result(124, "", f"timed out after {timeout}s")
        except OSError as e:
            res = Result(126, "", str(e))
        self.log.write(f"  -> exit {res.code}\n{res.text[-4000:]}" if res.text else f"  -> exit {res.code}")
        return res


# ---------------------------------------------------------------- which (with well-known dirs)

def extra_bin_dirs() -> List[Path]:
    h = home()
    dirs = [h / ".local" / "bin", h / ".cargo" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
            h / ".local" / "share" / "fnm", h / "Library" / "Application Support" / "fnm"]
    if IS_WINDOWS:
        la = Path(os.environ.get("LOCALAPPDATA", h / "AppData" / "Local"))
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        ad = Path(os.environ.get("APPDATA", h / "AppData" / "Roaming"))
        dirs += [la / "Microsoft" / "WinGet" / "Links", la / "Microsoft" / "WindowsApps",
                 la / "Programs" / "Microsoft VS Code" / "bin", pf / "Git" / "cmd", pf / "GitHub CLI",
                 la / "Pandoc", pf / "Pandoc", ad / "fnm", la / "fnm"]
    return dirs


def which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    exts = [""]
    if IS_WINDOWS:
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";")] + [""]
    for d in extra_bin_dirs():
        for ext in exts:
            p = d / (name + ext)
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


# ---------------------------------------------------------------- versions

def parse_version(v: str) -> tuple:
    v = str(v).strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)", v)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def _cmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def version_satisfies(version: str, spec: Optional[str]) -> bool:
    """spec like '>=3.9,<3.14', '3.12' (prefix match), '*' or None."""
    if not spec or spec in ("*", "latest", "managed"):
        return True
    if str(version).lower() == "lts" or str(spec).lower() == "lts":
        return True
    v = parse_version(version)
    if not v:
        return False
    for part in str(spec).split(","):
        part = part.strip()
        m = re.match(r"^(>=|<=|==|!=|>|<|~=)?\s*(.+)$", part)
        op, target = m.group(1), parse_version(m.group(2))
        if not target:
            return False
        if op is None:  # prefix match: "3.12" matches 3.12.7
            if v[: len(target)] != target:
                return False
            continue
        c = _cmp(v, target)
        ok = {">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0, "==": c == 0, "!=": c != 0,
              "~=": c >= 0 and v[: len(target) - 1] == target[: len(target) - 1]}[op]
        if not ok:
            return False
    return True


def first_version(text: str) -> Optional[str]:
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- locking

class LockError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Lock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or "0")
                except (OSError, ValueError):
                    pid = 0
                if _pid_alive(pid):
                    raise LockError("Another env-wizard setup is already running. Wait for it to finish, then try again.")
                try:
                    self.path.unlink()  # stale lock from an interrupted run
                except FileNotFoundError:
                    pass

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- output

def emit(obj) -> None:
    """Print one JSON object per line (machine-readable output for Claude)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def uniq(seq: Iterable) -> list:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

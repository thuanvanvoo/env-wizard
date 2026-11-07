"""Install-method plumbing shared by all methods."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..util import IS_WINDOWS, Result, Runner, home, which


@dataclass
class Ctx:
    plat: str
    project: Path
    runner: Runner
    catalog: object
    run_id: str = "adhoc"
    admin: Dict = field(default_factory=dict)
    plan: Dict = field(default_factory=dict)
    gstate: object = None
    pstate: object = None


@dataclass
class Check:
    present: bool
    satisfies: bool = False
    version: Optional[str] = None
    path: Optional[str] = None
    detail: str = ""


@dataclass
class Outcome:
    status: str  # ok | failed | needs_admin | needs_user | unsupported | kept
    detail: str = ""
    version: Optional[str] = None
    path: Optional[str] = None
    paths_created: List[str] = field(default_factory=list)
    profile_edits: List[Dict] = field(default_factory=list)
    config_backups: List[str] = field(default_factory=list)
    details: Dict = field(default_factory=dict)
    handoff: Optional[Dict] = None
    error_text: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def fail(res: Result, what: str) -> Outcome:
    return Outcome("failed", detail=what, error_text=res.text[-2000:])


class Method:
    name = ""

    def check(self, ctx: Ctx, step: Dict) -> Check:
        raise NotImplementedError

    def install(self, ctx: Ctx, step: Dict) -> Outcome:
        raise NotImplementedError

    def uninstall(self, ctx: Ctx, rec: Dict) -> Outcome:
        return Outcome("kept", detail="Nothing to remove.")

    def smoke(self, ctx: Ctx, step: Dict) -> Optional[Outcome]:
        return default_smoke(ctx, step)


# ------------------------------------------------------------------ helpers

def uv_bin() -> Optional[str]:
    return which("uv")


def venv_python(project: Path) -> Path:
    return project / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def fnm_bin() -> Optional[str]:
    return which("fnm")


def node_cmd(ctx: Ctx, version: Optional[str], argv: List[str]) -> List[str]:
    """Run a node/npm/npx command under the right Node version (fnm if available)."""
    fnm = fnm_bin()
    if fnm and version:
        return [fnm, "exec", f"--using={version}", "--"] + argv
    exe = which(argv[0])
    return [exe or argv[0]] + argv[1:]


def default_smoke(ctx: Ctx, step: Dict) -> Optional[Outcome]:
    test = step.get("smoke_test")
    if not test:
        return None
    kind = test.get("kind")
    if kind == "python":
        uv = uv_bin()
        vp = venv_python(ctx.project)
        if vp.exists():
            argv = [str(vp), "-c", test["code"]]
        elif uv:
            argv = [uv, "run", "--no-project", "--python", step.get("version") or "3", "python", "-c", test["code"]]
        else:
            return Outcome("failed", detail="uv not found")
    elif kind == "node":
        argv = node_cmd(ctx, step.get("version"), ["node", "-e", test["code"]])
    elif kind == "command":
        exe = which(test["argv"][0])
        if not exe:
            return Outcome("failed", detail=f"{test['argv'][0]} not found after install")
        argv = [exe] + list(test["argv"][1:])
    else:
        return None
    res = ctx.runner.run(argv, cwd=ctx.project, timeout=300)
    if res.ok:
        return Outcome("ok", detail=(res.out.strip().splitlines() or [""])[-1][:200])
    return fail(res, "The quick test after installing didn't pass.")


def prepend_path(dir_: str) -> None:
    """Make a new bin dir visible to the rest of this run (no restart needed)."""
    cur = os.environ.get("PATH", "")
    if dir_ not in cur.split(os.pathsep):
        os.environ["PATH"] = dir_ + os.pathsep + cur


def local_bin() -> Path:
    return home() / ".local" / "bin"

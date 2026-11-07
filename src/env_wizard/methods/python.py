"""uv, Python, per-project virtual environments and Python packages."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List

from ..util import IS_WINDOWS, first_version, home, version_satisfies, which
from . import shellinit
from .base import Check, Ctx, Method, Outcome, default_smoke, fail, local_bin, prepend_path, uv_bin, venv_python


def _need_uv() -> Outcome:
    return Outcome("failed", detail="uv isn't installed yet, so Python can't be set up.")


class UvStandalone(Method):
    name = "uv-standalone"

    def check(self, ctx, step):
        uv = uv_bin()
        if not uv:
            return Check(False)
        res = ctx.runner.run([uv, "--version"], timeout=30)
        return Check(True, res.ok, first_version(res.out), uv)

    def install(self, ctx, step):
        ver = step.get("version") or "latest"
        base = "https://astral.sh/uv" + ("" if ver == "latest" else f"/{ver}")
        env = {"UV_NO_MODIFY_PATH": "1"}  # we add our own, removable PATH entry
        if IS_WINDOWS:
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                    f"irm {base}/install.ps1 | iex"]
        else:
            argv = ["sh", "-c", f"curl -LsSf {base}/install.sh | sh"]
        res = ctx.runner.run(argv, env=env, timeout=900)
        if not res.ok:
            return fail(res, "Couldn't download uv.")
        bindir = local_bin()
        prepend_path(str(bindir))
        exe = ".exe" if IS_WINDOWS else ""
        created = [str(bindir / f"uv{exe}"), str(bindir / f"uvx{exe}")]
        if IS_WINDOWS:
            created.append(str(bindir / f"uvw{exe}"))
        edits = shellinit.apply_snippet(ctx, "local-bin", win_dir=str(bindir))
        chk = self.check(ctx, step)
        if not chk.present:
            return Outcome("failed", detail="uv was downloaded but can't be found.", error_text=res.text[-2000:])
        return Outcome("ok", version=chk.version, path=chk.path, paths_created=[p for p in created if Path(p).exists()],
                       profile_edits=edits)

    def uninstall(self, ctx, rec):
        for p in rec.get("paths_created", []):
            try:
                Path(p).unlink()
            except FileNotFoundError:
                pass
        return Outcome("ok")


class UvPython(Method):
    name = "uv-python"

    def check(self, ctx, step):
        uv = uv_bin()
        if not uv:
            return Check(False)
        ver = step.get("version")
        # Run from home so a project's .venv is never mistaken for an installed Python.
        res = ctx.runner.run([uv, "python", "find", ver] if ver else [uv, "python", "find"], cwd=home(), timeout=60)
        if not res.ok or not res.out.strip():
            if ver:
                other = ctx.runner.run([uv, "python", "find"], cwd=home(), timeout=60)
                if other.ok and other.out.strip():
                    ov = first_version(ctx.runner.run([other.out.strip().splitlines()[-1], "--version"], timeout=30).text)
                    return Check(True, False, ov, detail="other-version")
            return Check(False)
        path = res.out.strip().splitlines()[-1]
        vres = ctx.runner.run([path, "--version"], timeout=30)
        v = first_version(vres.text)
        return Check(True, version_satisfies(v or "", ver), v, path)

    def install(self, ctx, step):
        uv = uv_bin()
        if not uv:
            return _need_uv()
        ver = step["version"]
        res = ctx.runner.run([uv, "python", "install", ver], cwd=home(), timeout=1800)
        if not res.ok:
            return fail(res, f"Couldn't install Python {ver}.")
        chk = self.check(ctx, step)
        return Outcome("ok", version=chk.version, path=chk.path, details={"uv_request": ver})

    def uninstall(self, ctx, rec):
        uv = uv_bin()
        req = rec.get("details", {}).get("uv_request") or rec.get("version")
        if not uv or not req:
            return Outcome("failed", detail="uv not found; can't remove Python.")
        res = ctx.runner.run([uv, "python", "uninstall", req], cwd=home(), timeout=600)
        return Outcome("ok") if res.ok else fail(res, "Couldn't remove Python.")


class UvVenv(Method):
    name = "uv-venv"

    def check(self, ctx, step):
        vp = venv_python(ctx.project)
        if not vp.exists():
            return Check(False)
        res = ctx.runner.run([str(vp), "--version"], timeout=30)
        v = first_version(res.text)
        return Check(True, bool(v) and version_satisfies(v, step.get("python_version")), v, str(vp))

    def install(self, ctx, step):
        uv = uv_bin()
        if not uv:
            return _need_uv()
        pyver = step.get("python_version") or "3"
        venv = ctx.project / ".venv"
        chk = self.check(ctx, step)
        if venv.exists() and not chk.satisfies:
            if not step.get("allow_recreate"):
                return Outcome("needs_user", detail=(
                    f"This project already has a Python environment (.venv) using Python {chk.version or 'unknown'}, "
                    f"but it needs Python {pyver}. Recreating it replaces the libraries installed in it."),
                    details={"question": "recreate_venv"})
            shutil.rmtree(venv)
        created = []
        if not venv.exists():
            res = ctx.runner.run([uv, "venv", "--python", pyver, str(venv)], cwd=ctx.project, timeout=900)
            if not res.ok:
                shutil.rmtree(venv, ignore_errors=True)  # no half-made environment left behind
                return fail(res, "Couldn't create the project's Python environment.")
            created.append(str(venv))
        pv = ctx.project / ".python-version"
        if not pv.exists():
            pv.write_text(pyver + "\n", encoding="utf-8")
            created.append(str(pv))
        chk = self.check(ctx, step)
        return Outcome("ok", version=chk.version, path=chk.path, paths_created=created,
                       details={"python_version_file": str(pv) if str(pv) in created else None})

    def uninstall(self, ctx, rec):
        for p in rec.get("paths_created", []):
            path = Path(p)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        return Outcome("ok")


class UvTool(Method):
    """A Python command-line tool installed in its own isolated environment (used by org overlays)."""
    name = "uv-tool"

    def check(self, ctx, step):
        uv = uv_bin()
        if not uv:
            return Check(False)
        res = ctx.runner.run([uv, "tool", "list"], timeout=60)
        pkg = step["params"]["package"].lower()
        for line in res.out.splitlines():
            parts = line.split()
            if parts and parts[0].lower() == pkg:
                v = first_version(line)
                return Check(True, version_satisfies(v or "", step.get("version")), v)
        return Check(False)

    def install(self, ctx, step):
        uv = uv_bin()
        if not uv:
            return _need_uv()
        pkg = step["params"]["package"]
        spec = f"{pkg}=={step['version']}" if step.get("version") else pkg
        res = ctx.runner.run([uv, "tool", "install", spec], timeout=1800)
        if not res.ok:
            return fail(res, f"Couldn't install {step['name']}.")
        prepend_path(str(local_bin()))
        return Outcome("ok", version=step.get("version"), details={"package": pkg})

    def uninstall(self, ctx, rec):
        uv = uv_bin()
        res = ctx.runner.run([uv, "tool", "uninstall", rec["details"]["package"]], timeout=600) if uv else None
        return Outcome("ok") if res and res.ok else Outcome("failed", detail="Couldn't remove the tool.")


def _installed_packages(ctx: Ctx) -> Dict[str, str]:
    uv, vp = uv_bin(), venv_python(ctx.project)
    if not uv or not vp.exists():
        return {}
    res = ctx.runner.run([uv, "pip", "list", "--python", str(vp), "--format", "json"], timeout=120)
    try:
        return {p["name"].lower().replace("_", "-"): p["version"] for p in json.loads(res.out or "[]")}
    except (ValueError, KeyError):
        return {}


class PythonPackages(Method):
    name = "python-packages"

    def _missing(self, ctx, step) -> List[Dict]:
        have = _installed_packages(ctx)
        return [p for p in step.get("packages", []) if have.get(p["name"].lower()) != p["version"]]

    def check(self, ctx, step):
        if not venv_python(ctx.project).exists():
            return Check(False)
        missing = self._missing(ctx, step)
        return Check(True, not missing, detail=", ".join(p["name"] for p in missing))

    def install(self, ctx, step):
        uv, vp = uv_bin(), venv_python(ctx.project)
        if not uv:
            return _need_uv()
        if not vp.exists():
            return Outcome("failed", detail="The project's Python environment is missing.")
        before = _installed_packages(ctx)
        missing = self._missing(ctx, step)
        if missing:
            res = ctx.runner.run([uv, "pip", "install", "--python", str(vp)] +
                                 [f"{p['name']}=={p['version']}" for p in missing], cwd=ctx.project, timeout=3600)
            if not res.ok:
                return fail(res, "Couldn't install the Python libraries.")
        new = [p["name"] for p in missing if p["name"].lower() not in before]
        changed = {p["name"]: before[p["name"].lower()] for p in missing if p["name"].lower() in before}
        return Outcome("ok", details={"packages": new, "previous_versions": changed})

    def uninstall(self, ctx, rec):
        uv, vp = uv_bin(), venv_python(ctx.project)
        pkgs = rec.get("details", {}).get("packages") or []
        if not pkgs or not vp.exists():
            return Outcome("ok", detail="Nothing left to remove.")
        res = ctx.runner.run([uv, "pip", "uninstall", "--python", str(vp)] + pkgs, cwd=ctx.project, timeout=600)
        return Outcome("ok") if res.ok else fail(res, "Couldn't remove the Python libraries.")

    def smoke(self, ctx, step):
        vp = venv_python(ctx.project)
        pkgs = step.get("packages", [])
        names = {p["name"].lower() for p in pkgs}
        imports = [p.get("import") or p["name"].replace("-", "_") for p in pkgs]
        code = "; ".join(f"import {m}" for m in imports) or "pass"
        if {"pandas", "openpyxl"} <= names:
            # The test the user cares about: can we open an Excel file?
            tmp = Path(tempfile.gettempdir()) / f"env-wizard-smoke-{ctx.run_id}.xlsx"
            code += (f"; import pandas as pd; p=r'{tmp}'; pd.DataFrame({{'region':['N','S'],'sales':[1,2]}})"
                     ".to_excel(p, index=False); assert pd.read_excel(p)['sales'].sum()==3; import os; os.remove(p)")
        code += "; print('libraries OK')"
        res = ctx.runner.run([str(vp), "-c", code], cwd=ctx.project, timeout=300)
        return Outcome("ok", detail="Opened a sample Excel file" if "pandas" in names and "openpyxl" in names
                       else "Libraries load correctly") if res.ok else fail(res, "The libraries didn't load.")


def deps_fingerprint(project: Path, files: List[str]) -> str:
    h = hashlib.sha256()
    for name in files:
        p = project / name
        if p.is_file():
            h.update(name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()


PY_DEP_FILES = ["uv.lock", "pyproject.toml", "requirements.txt"]


def python_dep_files(project: Path) -> List[str]:
    return [f for f in PY_DEP_FILES if (project / f).is_file()]


class ProjectPythonDeps(Method):
    name = "project-python-deps"

    def check(self, ctx, step):
        if not venv_python(ctx.project).exists():
            return Check(False)
        rec = ctx.pstate.get(step["id"]) if ctx.pstate else None
        fp = deps_fingerprint(ctx.project, python_dep_files(ctx.project))
        ok = bool(rec) and rec.get("details", {}).get("fingerprint") == fp
        return Check(True, ok)

    def install(self, ctx, step):
        uv, vp = uv_bin(), venv_python(ctx.project)
        if not uv:
            return _need_uv()
        files = python_dep_files(ctx.project)
        note = ""
        if "uv.lock" in files and "pyproject.toml" in files:
            argv = [uv, "sync", "--frozen"]
        elif "pyproject.toml" in files:
            argv = [uv, "sync"]
            note = "Created uv.lock to record exact versions."
        elif "requirements.txt" in files:
            req = (ctx.project / "requirements.txt").read_text(encoding="utf-8", errors="replace")
            argv = [uv, "pip", "install", "--python", str(vp), "-r", "requirements.txt"]
            if "--hash=" in req:
                argv.append("--require-hashes")
        else:
            return Outcome("ok", detail="This project doesn't list any Python requirements.")
        res = ctx.runner.run(argv, cwd=ctx.project, timeout=3600)
        if not res.ok:
            return fail(res, "Couldn't install this project's Python requirements.")
        return Outcome("ok", detail=note, details={"fingerprint": deps_fingerprint(ctx.project, python_dep_files(ctx.project)),
                                                   "files": files})

    def uninstall(self, ctx, rec):
        return Outcome("ok", detail="Removed together with the project's Python environment.")

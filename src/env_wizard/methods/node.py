"""fnm, Node.js and npm packages."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Dict, List

from ..util import first_version, home, version_satisfies, which
from . import shellinit
from .base import Check, Ctx, Method, Outcome, fail, fnm_bin, node_cmd, prepend_path
from .python import deps_fingerprint


class FnmScript(Method):
    """Linux/WSL: fnm's official installer into ~/.local/share/fnm (no admin)."""
    name = "fnm-script"

    def check(self, ctx, step):
        p = fnm_bin()
        return Check(bool(p), bool(p), path=p)

    def install(self, ctx, step):
        d = home() / ".local" / "share" / "fnm"
        existed = d.exists()
        res = ctx.runner.run(["bash", "-c", f'curl -fsSL https://fnm.vercel.app/install | bash -s -- '
                                           f'--install-dir "{d}" --skip-shell'], timeout=900)
        if not res.ok:
            return fail(res, "Couldn't download fnm.")
        prepend_path(str(d))
        edits = shellinit.apply_snippet(ctx, "fnm-path")
        return Outcome("ok", path=fnm_bin(), paths_created=[] if existed else [str(d)], profile_edits=edits)

    def uninstall(self, ctx, rec):
        for p in rec.get("paths_created", []):
            shutil.rmtree(p, ignore_errors=True)
        return Outcome("ok")


def _fnm_versions(ctx: Ctx) -> List[str]:
    fnm = fnm_bin()
    if not fnm:
        return []
    res = ctx.runner.run([fnm, "list"], timeout=60)
    return re.findall(r"v(\d+\.\d+\.\d+)", res.out)


def _fnm_has_default(ctx: Ctx) -> bool:
    fnm = fnm_bin()
    return bool(fnm) and "default" in ctx.runner.run([fnm, "list"], timeout=60).out


class FnmNode(Method):
    name = "fnm-node"

    def check(self, ctx, step):
        want = step.get("version")
        for v in _fnm_versions(ctx):
            if version_satisfies(v, want):
                return Check(True, True, v, detail="fnm")
        node = which("node")
        if node:
            v = first_version(ctx.runner.run([node, "--version"], timeout=30).out)
            if v:
                return Check(True, version_satisfies(v, want), v, node, detail="system")
        return Check(False)

    def install(self, ctx, step):
        fnm = fnm_bin()
        if not fnm:
            return Outcome("failed", detail="fnm isn't installed yet, so Node.js can't be set up.")
        want = step["version"]
        had_node = bool(which("node")) or _fnm_has_default(ctx)
        res = ctx.runner.run([fnm, "install", want], timeout=1800)
        if not res.ok:
            return fail(res, f"Couldn't install Node.js {want}.")
        set_default = False
        if not had_node:
            # Only make it the default when the user had no Node at all; never hijack an existing one.
            set_default = ctx.runner.run([fnm, "default", want], timeout=60).ok
        installed = [v for v in _fnm_versions(ctx) if version_satisfies(v, want)]
        resolved = installed[-1] if installed else None
        return Outcome("ok", version=resolved, details={"fnm_version": resolved, "set_default": set_default})

    def uninstall(self, ctx, rec):
        fnm = fnm_bin()
        v = rec.get("details", {}).get("fnm_version")
        if not fnm or not v:
            return Outcome("failed", detail="fnm not found; can't remove Node.js.")
        res = ctx.runner.run([fnm, "uninstall", v], timeout=600)
        return Outcome("ok") if res.ok else fail(res, "Couldn't remove Node.js.")


def _read_pkg_json(project: Path) -> Dict:
    try:
        return json.loads((project / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _installed_version(project: Path, name: str):
    try:
        return json.loads((project / "node_modules" / name / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def skipped_install_scripts(project: Path) -> List[str]:
    """Packages that wanted to run install scripts (we install with --ignore-scripts)."""
    out = []
    nm = project / "node_modules"
    if not nm.is_dir():
        return out
    for pj in list(nm.glob("*/package.json")) + list(nm.glob("@*/*/package.json")):
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        scripts = data.get("scripts") or {}
        if any(k in scripts for k in ("preinstall", "install", "postinstall")):
            out.append(data.get("name") or pj.parent.name)
    return sorted(out)


def _remove_paths(paths: List[str]) -> None:
    for p in paths:
        path = Path(p)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


class NpmPackages(Method):
    name = "npm-packages"

    def _missing(self, ctx, step):
        return [p for p in step.get("packages", []) if _installed_version(ctx.project, p["name"]) != p["version"]]

    def check(self, ctx, step):
        if not (ctx.project / "package.json").exists():
            return Check(False)
        missing = self._missing(ctx, step)
        return Check(True, not missing, detail=", ".join(p["name"] for p in missing))

    def install(self, ctx, step):
        created = []
        pj = ctx.project / "package.json"
        if not pj.exists():
            slug = re.sub(r"[^a-z0-9\-]", "-", ctx.project.name.lower()).strip("-") or "my-project"
            pj.write_text(json.dumps({"name": slug, "private": True, "version": "0.0.0"}, indent=2) + "\n", encoding="utf-8")
            created += [str(pj), str(ctx.project / "package-lock.json"), str(ctx.project / "node_modules")]
        nvmrc = ctx.project / ".nvmrc"
        if not nvmrc.exists() and step.get("node_version"):
            nvmrc.write_text(step["node_version"] + "\n", encoding="utf-8")
            created.append(str(nvmrc))
        missing = self._missing(ctx, step)
        new = [p["name"] for p in missing if not _installed_version(ctx.project, p["name"])]
        if missing:
            argv = node_cmd(ctx, step.get("node_version"),
                            ["npm", "install", "--ignore-scripts", "--save-exact", "--no-fund", "--no-audit"] +
                            [f"{p['name']}@{p['version']}" for p in missing])
            res = ctx.runner.run(argv, cwd=ctx.project, timeout=3600)
            if not res.ok:
                _remove_paths(created)  # leave the project exactly as we found it
                return fail(res, "Couldn't install the JavaScript libraries.")
        return Outcome("ok", paths_created=created,
                       details={"packages": new, "skipped_scripts": skipped_install_scripts(ctx.project)})

    def uninstall(self, ctx, rec):
        created = rec.get("paths_created", [])
        pj = str(ctx.project / "package.json")
        if pj in created:
            _remove_paths(created)
            return Outcome("ok")
        pkgs = rec.get("details", {}).get("packages") or []
        if pkgs:
            res = ctx.runner.run(node_cmd(ctx, None, ["npm", "uninstall"] + pkgs), cwd=ctx.project, timeout=900)
            if not res.ok:
                return fail(res, "Couldn't remove the JavaScript libraries.")
        for p in created:
            if Path(p).is_file():
                Path(p).unlink()
        return Outcome("ok")

    def smoke(self, ctx, step):
        names = [p["name"] for p in step.get("packages", [])]
        missing = [n for n in names if not (ctx.project / "node_modules" / n).is_dir()]
        if missing:
            return Outcome("failed", detail="Missing after install: " + ", ".join(missing))
        return Outcome("ok", detail="Libraries are in place")


NODE_DEP_FILES = ["package-lock.json", "package.json", "pnpm-lock.yaml", "yarn.lock"]


class ProjectNodeDeps(Method):
    name = "project-node-deps"

    def check(self, ctx, step):
        if not (ctx.project / "node_modules").is_dir():
            return Check(False)
        rec = ctx.pstate.get(step["id"]) if ctx.pstate else None
        fp = deps_fingerprint(ctx.project, NODE_DEP_FILES)
        return Check(True, bool(rec) and rec.get("details", {}).get("fingerprint") == fp)

    def install(self, ctx, step):
        p = ctx.project
        if (p / "pnpm-lock.yaml").exists() or (p / "yarn.lock").exists():
            tool = "pnpm" if (p / "pnpm-lock.yaml").exists() else "Yarn"
            return Outcome("needs_user", detail=f"This project uses {tool}, which env-wizard doesn't support yet. "
                                                f"Ask the project's owner how to install its requirements.")
        if not (p / "package.json").exists():
            return Outcome("ok", detail="This project doesn't list any JavaScript requirements.")
        sub = ["npm", "ci"] if (p / "package-lock.json").exists() else ["npm", "install"]
        res = ctx.runner.run(node_cmd(ctx, step.get("node_version"), sub + ["--ignore-scripts", "--no-fund", "--no-audit"]),
                             cwd=p, timeout=3600)
        if not res.ok:
            return fail(res, "Couldn't install this project's JavaScript requirements.")
        return Outcome("ok", details={"fingerprint": deps_fingerprint(p, NODE_DEP_FILES),
                                      "skipped_scripts": skipped_install_scripts(p)})

    def uninstall(self, ctx, rec):
        return Outcome("kept", detail="Project requirements are left in place (they belong to the project).")


def run_install_scripts(ctx: Ctx, packages: List[str], node_version=None) -> Outcome:
    """Re-run install scripts for named packages, only after the user agreed (SPEC §5.6)."""
    res = ctx.runner.run(node_cmd(ctx, node_version, ["npm", "rebuild"] + packages), cwd=ctx.project, timeout=1800)
    return Outcome("ok") if res.ok else fail(res, "The install scripts failed.")

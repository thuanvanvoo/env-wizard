"""OS package managers: Homebrew (macOS), winget (Windows), and manual hand-offs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

from ..util import first_version, which
from .base import Check, Ctx, Method, Outcome, fail

# Hand-off recipes live in code, never in catalog files (catalog entries cannot contain commands).
RECIPES = {
    "homebrew": {
        "why": "Homebrew installs into a system folder, so your Mac asks for your password once. "
               "Claude never sees or types your password.",
        "steps": [
            {"label": "Install Apple's command line tools (a window pops up: click Install, then wait)",
             "command": "xcode-select --install", "skip_if": "xcode-clt"},
            {"label": "Install Homebrew (it asks for your Mac password; typing shows no dots, that's normal)",
             "command": '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'},
        ],
        "after": "When it says \"Installation successful\", come back and say done.",
    },
}


def _xcode_clt_present(ctx: Ctx) -> bool:
    return ctx.runner.run(["xcode-select", "-p"], timeout=20).ok


class HandoffCheck(Method):
    """Something only the user can install (needs their password). We explain, hand off, then verify."""
    name = "handoff-check"

    def check(self, ctx, step):
        path = which(step["params"]["bin"])
        return Check(present=bool(path), satisfies=bool(path), path=path)

    def install(self, ctx, step):
        recipe = RECIPES[step["params"]["recipe"]]
        steps = []
        for s in recipe["steps"]:
            if s.get("skip_if") == "xcode-clt" and _xcode_clt_present(ctx):
                continue
            steps.append({"label": s["label"], "command": s["command"]})
        return Outcome("needs_admin", detail=recipe["why"],
                       handoff={"why": recipe["why"], "steps": steps, "after": recipe["after"]})

    def uninstall(self, ctx, rec):
        return Outcome("kept", detail="Package managers are kept so other apps that use them keep working.")


class CheckOnly(Method):
    name = "check-only"

    def check(self, ctx, step):
        path = which(step["params"]["bin"])
        return Check(present=bool(path), satisfies=bool(path), path=path)

    def install(self, ctx, step):
        return Outcome("needs_user", detail=step["params"]["instructions"],
                       handoff={"why": step["params"]["instructions"], "steps": [], "after": "Then say done."})

    def uninstall(self, ctx, rec):
        return Outcome("kept", detail="Built into Windows; nothing to remove.")


class _Brew(Method):
    cask = False

    def _brew(self) -> Optional[str]:
        return which("brew")

    def _pkg(self, step) -> str:
        return step["params"]["cask" if self.cask else "formula"]

    def check(self, ctx, step):
        p = step["params"]
        if p.get("bin"):
            path = which(p["bin"])
            if path:
                res = ctx.runner.run([path, "--version"], timeout=30)
                return Check(True, True, first_version(res.text), path)
        if p.get("app"):
            for base in (Path("/Applications"), Path.home() / "Applications"):
                if (base / p["app"]).exists():
                    return Check(True, True, None, str(base / p["app"]))
        brew = self._brew()
        if not brew:
            return Check(False)
        argv = [brew, "list", "--versions"] + (["--cask"] if self.cask else []) + [self._pkg(step)]
        res = ctx.runner.run(argv, timeout=60)
        if res.ok and res.out.strip():
            return Check(True, True, first_version(res.out))
        return Check(False)

    def install(self, ctx, step):
        brew = self._brew()
        if not brew:
            return Outcome("failed", detail="Homebrew isn't available yet.")
        argv = [brew, "install"] + (["--cask"] if self.cask else []) + [self._pkg(step)]
        res = ctx.runner.run(argv, env={"HOMEBREW_NO_AUTO_UPDATE": "1", "NONINTERACTIVE": "1",
                                        "HOMEBREW_NO_INSTALL_CLEANUP": "1"}, timeout=3600)
        if not res.ok:
            return fail(res, f"Homebrew couldn't install {step['name']}.")
        chk = self.check(ctx, step)
        return Outcome("ok", version=chk.version, path=chk.path,
                       details={"manager": "brew", "package": self._pkg(step), "cask": self.cask})

    def uninstall(self, ctx, rec):
        brew = self._brew()
        d = rec.get("details", {})
        if not brew or not d.get("package"):
            return Outcome("failed", detail="Homebrew not found; can't remove.")
        argv = [brew, "uninstall"] + (["--cask"] if d.get("cask") else []) + [d["package"]]
        res = ctx.runner.run(argv, timeout=900)
        return Outcome("ok") if res.ok else fail(res, "Homebrew couldn't remove it.")


class BrewFormula(_Brew):
    name = "brew-formula"


class BrewCask(_Brew):
    name = "brew-cask"
    cask = True


class Winget(Method):
    name = "winget"
    common = ["--exact", "--accept-source-agreements", "--disable-interactivity"]

    def check(self, ctx, step):
        p = step["params"]
        if p.get("bin"):
            path = which(p["bin"])
            if path:
                res = ctx.runner.run([path, "--version"], timeout=30)
                return Check(True, True, first_version(res.text), path)
        wg = which("winget")
        if not wg:
            return Check(False)
        res = ctx.runner.run([wg, "list", "--id", p["package_id"]] + self.common, timeout=120)
        if res.ok and p["package_id"].lower() in res.out.lower():
            m = re.search(re.escape(p["package_id"]) + r"\s+(\S+)", res.out, re.I)
            return Check(True, True, m.group(1) if m else None)
        return Check(False)

    def install(self, ctx, step):
        p = step["params"]
        wg = which("winget")
        if not wg:
            return Outcome("failed", detail="winget isn't available yet.")
        argv = [wg, "install", "--id", p["package_id"], "--silent", "--accept-package-agreements"] + self.common
        if p.get("scope"):
            argv += ["--scope", p["scope"]]
        res = ctx.runner.run(argv, timeout=3600)
        # winget exit code for "already installed" is 0x8A15002B
        if not res.ok and "already installed" not in res.text.lower():
            return fail(res, f"winget couldn't install {step['name']}.")
        chk = self.check(ctx, step)
        return Outcome("ok", version=chk.version, path=chk.path,
                       details={"manager": "winget", "package": p["package_id"]})

    def uninstall(self, ctx, rec):
        wg = which("winget")
        pkg = rec.get("details", {}).get("package")
        if not wg or not pkg:
            return Outcome("failed", detail="winget not found; can't remove.")
        res = ctx.runner.run([wg, "uninstall", "--id", pkg, "--silent"] + self.common, timeout=900)
        return Outcome("ok") if res.ok else fail(res, "winget couldn't remove it.")


def admin_handoff_winget(step: Dict) -> Dict:
    p = step["params"]
    cmd = f"winget install --id {p['package_id']} --exact --accept-package-agreements --accept-source-agreements"
    return {
        "why": f"{step['name']} installs for everyone on this computer, so Windows asks for permission (a "
               "\"Do you want to allow this app to make changes?\" box). Click Yes.",
        "steps": [{"label": "Open PowerShell (Start menu, type PowerShell, press Enter) and paste this",
                   "command": cmd}],
        "after": "When it finishes, come back and say done.",
    }


def admin_handoff_brew(step: Dict) -> Dict:
    p = step["params"]
    flag = "--cask " if "cask" in p else ""
    cmd = f"brew install {flag}{p.get('cask') or p.get('formula')}"
    return {"why": f"{step['name']} needs your Mac password to install.",
            "steps": [{"label": "Open Terminal and paste this", "command": cmd}],
            "after": "When it finishes, come back and say done."}

"""Turns requirements (profiles, chat items, manifests) into an ordered, explainable plan."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import methods
from .catalog import Catalog
from .methods.base import Ctx
from .methods.node import NODE_DEP_FILES
from .methods.python import python_dep_files
from .state import state_key
from .util import data_dir, digest, new_id, now_iso, read_json, version_satisfies, write_json

CATEGORY_RANK = {"manager": 0, "runtime": 1, "cli": 2, "app": 3, "project": 4, "package": 5, "cc-config": 6, "account": 7}
DISK_MARGIN = 1.2


@dataclass
class Request:
    profiles: List[str] = field(default_factory=list)
    items: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    packages: Dict[str, List[str]] = field(default_factory=lambda: {"python": [], "npm": []})
    manifest: Optional[Dict] = None
    project_deps: bool = False
    source: str = "chat"


def _add(wanted: Dict, order: List, iid: str, version: Optional[str], source: str):
    if iid not in wanted:
        wanted[iid] = {"version": None, "sources": []}
        order.append(iid)
    if version:
        wanted[iid]["version"] = version
    if source not in wanted[iid]["sources"]:
        wanted[iid]["sources"].append(source)


def build_plan(catalog: Catalog, det: Dict, ctx: Ctx, req: Request) -> Dict:
    plat = det["platform"]["os"]
    wanted: Dict[str, Dict] = {}
    order: List[str] = []
    packages: Dict[str, Dict[str, str]] = {"python": {}, "npm": {}}  # name -> source
    declined: List[Dict] = []
    notes: List[str] = []
    claude_md: List[str] = []
    prompts: List[str] = []

    profiles = list(req.profiles)
    if req.manifest:
        profiles += [p for p in req.manifest["profiles"] if p not in profiles]
        notes += req.manifest.get("notes", [])

    # 1. Collect what was asked for
    for pid in profiles:
        prof = catalog.profiles.get(pid)
        if not prof:
            declined.append({"request": pid, "reason": "unknown_profile", "suggestions": sorted(catalog.profiles)})
            continue
        for iid in prof.items:
            _add(wanted, order, iid, None, f"profile:{pid}")
        for eco, names in prof.packages.items():
            for n in names:
                packages[eco].setdefault(n.lower(), f"profile:{pid}")
        if prof.claude_md:
            claude_md.append(prof.claude_md)
        prompts += prof.try_prompts

    explicit = list(req.items)
    if req.manifest:
        explicit += [(i, v) for i, v in req.manifest["items"]]
        for eco, names in req.manifest["packages"].items():
            for n in names:
                packages[eco].setdefault(n, "manifest")
    src_explicit = "manifest" if req.manifest else req.source
    for eco, names in (req.packages or {}).items():
        for n in names:
            packages.setdefault(eco, {}).setdefault(n.lower(), req.source)
    for iid, ver in explicit:
        if iid.startswith("package:"):
            _, eco, name = (iid.split(":", 2) + [""])[:3]
            if eco in packages and name:
                packages[eco].setdefault(name.lower(), src_explicit)
            continue
        if not catalog.get(iid):
            declined.append({"request": iid, "reason": "not_in_catalog", "suggestions": catalog.suggest(iid)})
            continue
        _add(wanted, order, iid, ver, src_explicit)

    for eco in ("python", "npm"):
        for name, src in list(packages[eco].items()):
            if name not in catalog.packages[eco]:
                declined.append({"request": f"{eco} library '{name}'", "reason": "not_on_allow_list",
                                 "suggestions": catalog.suggest(name)})
                packages[eco].pop(name)
    if packages["python"]:
        _add(wanted, order, "python-packages", None, "libraries")
    if packages["npm"]:
        _add(wanted, order, "npm-packages", None, "libraries")

    if req.project_deps or (req.manifest and req.manifest.get("project_deps")):
        if python_dep_files(ctx.project):
            _add(wanted, order, "project-python", None, "project files")
        if (ctx.project / "package.json").exists() or any((ctx.project / f).exists() for f in NODE_DEP_FILES):
            _add(wanted, order, "project-node", None, "project files")

    # 2. Version requests must be allowed
    for iid in list(order):
        item, ver = catalog.get(iid), wanted[iid]["version"]
        if ver and item.allowed_versions and not version_satisfies(ver, item.allowed_versions):
            declined.append({"request": f"{item.name} {ver}", "reason": "version_not_allowed",
                             "allowed": item.allowed_versions})
            wanted[iid]["version"] = None

    # 3. Swap admin-only items for user-space alternatives when this user can't get admin rights
    admin = det.get("admin", {})
    for iid in list(order):
        item = catalog.get(iid)
        if item.requires_admin.get(plat) and not admin.get("can_admin") and item.user_space_alternative:
            alt = item.user_space_alternative
            notes.append(f"You don't have admin rights, so {catalog.get(alt).name} is used instead of {item.name}.")
            _add(wanted, order, alt, wanted[iid]["version"], *wanted[iid]["sources"][:1])
            order.remove(iid)
            wanted.pop(iid)

    # 4. Dependency closure
    queue = list(order)
    while queue:
        iid = queue.pop(0)
        item = catalog.get(iid)
        for dep in item.deps_for(plat):
            if dep not in wanted:
                _add(wanted, order, dep, None, f"needed by {item.name}")
                queue.append(dep)

    # 5. Topological order (managers first, Claude config last)
    steps_order = _toposort(catalog, order, plat)

    # 6. Build steps and check what is already there
    versions: Dict[str, str] = {}
    steps: List[Dict] = []
    status_by_id: Dict[str, str] = {}
    for iid in steps_order:
        item = catalog.get(iid)
        entry = item.platform_entry(plat)
        ver = wanted[iid]["version"] or item.default_version
        if ver:
            versions[iid] = ver
        step = {
            "id": iid, "name": item.name, "summary": item.summary, "category": item.category,
            "scope": item.scope, "version": ver, "sources": wanted[iid]["sources"],
            "size_mb": item.size_mb, "est_minutes": item.est_minutes, "smoke_test": item.smoke_test,
            "license_note": item.license_note, "depends_on": item.deps_for(plat), "shell_init": item.shell_init,
        }
        if iid == "python-packages":
            step["packages"] = [_pkg(catalog, "python", n) for n in packages["python"]]
            step["size_mb"] = 40 * len(step["packages"])
        if iid == "npm-packages":
            step["packages"] = [_pkg(catalog, "npm", n) for n in packages["npm"]]
            step["size_mb"] = 10 * len(step["packages"])
        if entry is None:
            step.update(action="unsupported", method=None,
                        reason=f"{item.name} isn't available on {_plat_name(plat)} yet.")
            steps.append(step)
            status_by_id[iid] = "unsupported"
            continue
        blocked_by = [d for d in step["depends_on"] if status_by_id.get(d) in ("unsupported", "blocked")]
        step["method"], step["params"] = entry["method"], {k: v for k, v in entry.items() if k != "method"}
        if blocked_by:
            step.update(action="blocked", reason="Needs " + ", ".join(catalog.get(d).name for d in blocked_by)
                        + ", which isn't available here.")
            steps.append(step)
            status_by_id[iid] = "blocked"
            continue
        steps.append(step)
    _link_versions(steps, versions)

    for step in steps:
        if step.get("action"):
            continue
        method = methods.get(step["method"])
        chk = method.check(ctx, step)
        rec = (ctx.pstate if step["scope"] == "project" else ctx.gstate).get(state_key(step["id"], step.get("version")))
        if chk.satisfies:
            step["action"] = "reuse"
            step["found_version"] = chk.version
            step["already_by"] = "env-wizard" if rec and rec.get("installed_by_plugin") else "you"
            step["size_mb"] = 0
            step["est_minutes"] = 0
        else:
            step["action"] = "install"
            if chk.present and chk.version and step["category"] == "runtime":
                step["alongside"] = chk.version  # existing version is left untouched
            if chk.detail and step["id"].endswith("-packages"):
                step["missing"] = chk.detail
            if step["method"] == "handoff-check" or step["method"] == "check-only":
                step["action"] = "handoff"
            elif catalog.get(step["id"]).requires_admin.get(plat) and not admin.get("elevated"):
                step["action"] = "handoff"
                step["needs_admin"] = True
            if hasattr(method, "preview"):
                step["changes"] = method.preview(ctx, step)
    steps = _prune_unneeded(catalog, steps, wanted, plat)
    total_mb = sum(s["size_mb"] for s in steps if s["action"] in ("install", "handoff"))
    total_min = sum(s["est_minutes"] for s in steps if s["action"] in ("install", "handoff"))

    _log_declined(declined, ctx.project)
    preflight = _preflight(det, total_mb, steps)
    plan = {
        "plan_id": new_id("plan"),
        "created": now_iso(),
        "platform": plat,
        "project": str(ctx.project),
        "manifest": req.manifest.get("path") if req.manifest else None,
        "profiles": profiles,
        "steps": steps,
        "declined": declined,
        "notes": notes,
        "totals": {"download_mb": total_mb, "minutes": total_min,
                   "needs_password": any(s.get("action") == "handoff" for s in steps)},
        "preflight": preflight,
        "claude_md": claude_md,
        "try_prompts": prompts[:3],
        "license_notes": [s["license_note"] for s in steps if s.get("license_note") and s["action"] == "install"],
    }
    if det["platform"].get("rosetta"):
        notes.append("Claude Code is running in Intel mode (Rosetta) on an Apple Silicon Mac. Tools will be "
                     "installed for Apple Silicon; restarting Claude Code natively is recommended.")
    if det["platform"].get("support_note"):
        notes.append(det["platform"]["support_note"])
    return plan


def _prune_unneeded(catalog: Catalog, steps: List[Dict], wanted: Dict, plat: str) -> List[Dict]:
    """Drop helpers that were only pulled in to install something that is already there
    (e.g. fnm when the user's own Node.js already satisfies the plan)."""
    by_id = {s["id"]: s for s in steps}
    needed = {iid for iid, w in wanted.items() if any(not src.startswith("needed by") for src in w["sources"])}
    queue = [i for i in needed if i in by_id]
    while queue:
        iid = queue.pop()
        if by_id[iid]["action"] in ("reuse",):
            continue
        for dep in catalog.get(iid).deps_for(plat):
            if dep not in needed and dep in by_id:
                needed.add(dep)
                queue.append(dep)
    return [s for s in steps if s["id"] in needed]


def _log_declined(declined: List[Dict], project: Path) -> None:
    """Off-catalog requests are kept locally so the catalog can grow where people need it (SPEC §5.3)."""
    if not declined:
        return
    with open(data_dir() / "requests.log", "a", encoding="utf-8") as f:
        for d in declined:
            f.write(json.dumps({"time": now_iso(), "request": d["request"], "reason": d["reason"]}) + "\n")


def _pkg(catalog, eco, name) -> Dict:
    p = catalog.packages[eco][name]
    return {"name": p.name, "version": p.version, "summary": p.summary, "import": p.import_name}


def _plat_name(plat: str) -> str:
    return {"macos": "Mac", "windows": "Windows", "linux": "Linux", "wsl": "WSL"}.get(plat, plat)


def _link_versions(steps: List[Dict], versions: Dict[str, str]) -> None:
    for s in steps:
        if s["id"] in ("python-env",):
            s["python_version"] = versions.get("python")
        if s["id"] in ("npm-packages", "project-node") or s["id"].startswith("mcp:"):
            s["node_version"] = versions.get("node")


def _toposort(catalog: Catalog, ids: List[str], plat: str) -> List[str]:
    idset = set(ids)
    pos = {iid: i for i, iid in enumerate(catalog.order)}
    deps = {iid: [d for d in catalog.get(iid).deps_for(plat) if d in idset] for iid in ids}

    def key(iid):
        item = catalog.get(iid)
        last = 1 if iid == "claude-md" else 0
        return (last, CATEGORY_RANK.get(item.category, 9), pos.get(iid, 10_000), iid)

    done: List[str] = []
    remaining = set(ids)
    while remaining:
        ready = sorted([i for i in remaining if all(d in done for d in deps[i])], key=key)
        if not ready:
            raise ValueError("dependency cycle in catalog: " + ", ".join(sorted(remaining)))
        done.append(ready[0])
        remaining.remove(ready[0])
    return done


def _preflight(det: Dict, total_mb: int, steps: List[Dict]) -> Dict:
    problems = []
    if not det["platform"]["supported"]:
        problems.append({"kind": "unsupported_os", "message": det["platform"]["support_note"]})
    free = min(det["disk_free_mb"].values()) if det.get("disk_free_mb") else None
    need = int(total_mb * DISK_MARGIN) + 100 if total_mb else 0
    if free is not None and need and free < need:
        problems.append({"kind": "low_disk", "message": f"Needs about {need} MB free, but only {free} MB is available.",
                         "need_mb": need, "free_mb": free})
    if det.get("network", {}).get("online") is False and any(s["action"] == "install" for s in steps):
        problems.append({"kind": "offline", "message": "This computer can't reach the internet, so nothing can be downloaded."})
    proxy = det.get("proxy", {})
    return {"ok": not problems, "problems": problems, "behind_proxy": proxy.get("proxy") or proxy.get("custom_ca")}


# ------------------------------------------------------------------ persistence

def plans_dir() -> Path:
    return data_dir() / "plans"


def save_plan(plan: Dict) -> Path:
    path = plans_dir() / f"{plan['plan_id']}.json"
    write_json(path, plan)
    (plans_dir() / f"{plan['plan_id']}.sha256").write_text(digest(plan))
    return path


class PlanError(ValueError):
    pass


def load_plan(plan_id: str) -> Dict:
    path = plans_dir() / f"{plan_id}.json"
    plan = read_json(path, None)
    if not plan:
        raise PlanError(f"No plan called {plan_id}. Make a new plan first.")
    sha = plans_dir() / f"{plan_id}.sha256"
    if not sha.exists() or sha.read_text().strip() != digest(plan):
        raise PlanError("This plan was changed after it was shown, so it won't run. Make a new plan.")
    return plan


def plan_view(plan: Dict) -> Dict:
    """Compact version for Claude to turn into the plain-language plan screen."""
    groups = {"install": [], "reuse": [], "handoff": [], "unsupported": [], "blocked": []}
    for s in plan["steps"]:
        row = {"id": s["id"], "name": s["name"], "why": s["summary"], "version": s.get("version")}
        for k in ("alongside", "found_version", "already_by", "reason", "changes", "missing", "needs_admin"):
            if s.get(k):
                row[k] = s[k]
        if s.get("packages"):
            row["libraries"] = [f"{p['name']} {p['version']}: {p['summary']}" for p in s["packages"]]
        groups.setdefault(s["action"], []).append(row)
    return {"plan_id": plan["plan_id"], "project": plan["project"], "will_install": groups["install"],
            "needs_your_password": groups["handoff"], "already_have": groups["reuse"],
            "not_available": groups["unsupported"] + groups["blocked"], "declined": plan["declined"],
            "totals": plan["totals"], "preflight": plan["preflight"], "notes": plan["notes"],
            "license_notes": plan["license_notes"]}

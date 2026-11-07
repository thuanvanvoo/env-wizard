"""Undo: remove only what env-wizard itself installed or changed (SPEC §6.3)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Optional

from . import methods
from .methods import shellinit
from .methods.base import Ctx
from .state import base_id, project_state


def _method_for(ctx: Ctx, iid: str, rec: Dict) -> Optional[str]:
    if rec.get("method"):
        return rec["method"]
    item = ctx.catalog.get(base_id(iid))
    entry = item.platform_entry(ctx.plat) if item else None
    return entry["method"] if entry else None


def _depends_on(deps: List[str], rec: Dict, key: str) -> bool:
    """Does a record (with these catalog deps) rely on the state entry `key` (e.g. 'python@3.12')?"""
    b, _, v = key.partition("@")
    if b not in deps:
        return False
    if not v:
        return True
    snap = rec.get("step") or {}
    want = snap.get("python_version") if b == "python" else snap.get("node_version") if b == "node" else None
    return want is None or str(want) == v


def _other_projects(ctx: Ctx, rec: Dict) -> List[str]:
    """Other projects that still rely on this global tool."""
    out = []
    for p in rec.get("used_by", []):
        if Path(p) == ctx.project:
            continue
        if Path(p).is_dir() and project_state(Path(p)).items:
            out.append(p)
    return out


def plan_undo(ctx: Ctx, target: str, include_shared: bool = False) -> Dict:
    """target: 'all', an item id, or a run id."""
    candidates = []
    for scope, state in (("project", ctx.pstate), ("global", ctx.gstate)):
        for iid, rec in state.items.items():
            match = target == "all" or target == iid or target == rec.get("run_id")
            if not match:
                continue
            if scope == "global" and target == "all" and str(ctx.project) not in rec.get("used_by", []) \
                    and rec.get("used_by"):
                continue  # 'undo all' in a project only touches global tools this project uses
            candidates.append({"id": iid, "scope": scope, "rec": rec})

    # Removing a tool also removes plugin-made things that depend on it (e.g. Python -> project env).
    ids = {c["id"] for c in candidates}
    changed = True
    while changed:
        changed = False
        for scope, state in (("project", ctx.pstate), ("global", ctx.gstate)):
            for iid, rec in state.items.items():
                if iid in ids or not rec.get("installed_by_plugin"):
                    continue
                item = ctx.catalog.get(base_id(iid))
                if item and any(_depends_on(item.deps_for(ctx.plat), rec, k) for k in ids):
                    candidates.append({"id": iid, "scope": scope, "rec": rec, "because": "depends on something being removed"})
                    ids.add(iid)
                    changed = True

    remove, keep, forget = [], [], []
    for c in candidates:
        item = ctx.catalog.get(base_id(c["id"]))
        rec, name = c["rec"], (item.name + (f" {c['id'].split('@', 1)[1]}" if "@" in c["id"] else "")) if item else c["id"]
        row = {"id": c["id"], "name": name, "scope": c["scope"]}
        if c.get("because"):
            row["because"] = c["because"]
        if not rec.get("installed_by_plugin"):
            row["reason"] = ("It was already on this computer before env-wizard, so it stays."
                             if rec.get("pre_existing") else "You installed it yourself, so it stays.")
            keep.append(row)
            forget.append(c)
            continue
        others = _other_projects(ctx, rec) if c["scope"] == "global" else []
        if others and not include_shared:
            row["reason"] = f"Still used by {len(others)} other project(s), so it stays."
            row["used_by"] = others
            keep.append(row)
            continue
        if _method_for(ctx, c["id"], rec) in ("handoff-check", "check-only"):
            row["reason"] = "Package managers are kept so other apps keep working."
            keep.append(row)
            continue
        remove.append(dict(row, rec=rec))
    order = _removal_order(ctx, remove)
    return {"remove": order, "keep": keep, "forget": forget}


def _removal_order(ctx: Ctx, rows: List[Dict]) -> List[Dict]:
    """Dependents first (e.g. libraries before the environment before Python)."""
    ids = {r["id"] for r in rows}
    out, remaining = [], list(rows)
    while remaining:
        for r in remaining:
            dependents = [o for o in remaining if o is not r and ctx.catalog.get(base_id(o["id"]))
                          and _depends_on(ctx.catalog.get(base_id(o["id"])).deps_for(ctx.plat), o["rec"], r["id"])]
            if not dependents:
                out.append(r)
                remaining.remove(r)
                break
        else:
            out += remaining
            break
    return out


def preview(plan: Dict) -> Dict:
    return {"will_remove": [{k: v for k, v in r.items() if k != "rec"} for r in plan["remove"]],
            "will_keep": plan["keep"]}


def execute(ctx: Ctx, plan: Dict) -> Dict:
    results = []
    for row in plan["remove"]:
        rec = row["rec"]
        method = _method_for(ctx, row["id"], rec)
        state = ctx.pstate if row["scope"] == "project" else ctx.gstate
        out = methods.get(method).uninstall(ctx, rec) if method else None
        ok = out is None or out.status in ("ok", "kept")
        for edit in rec.get("profile_edits", []):
            shellinit.undo_edit(ctx, edit)
        if ok:
            state.remove(row["id"])
        results.append({"id": row["id"], "name": row["name"], "status": "removed" if ok else "failed",
                        "detail": out.detail if out else ""})
    for c in plan["forget"]:
        # Only stop tracking; the tool itself is untouched.
        state = ctx.pstate if c["scope"] == "project" else ctx.gstate
        if c["scope"] == "project":
            state.remove(c["id"])
    if not ctx.pstate.items:
        shutil.rmtree(ctx.project / ".env-wizard", ignore_errors=True)
    return {"results": results, "kept": plan["keep"],
            "restart_needed": any(r["rec"].get("profile_edits") for r in plan["remove"])}

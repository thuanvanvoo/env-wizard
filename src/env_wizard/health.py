"""status, repair and the SessionStart health check (SPEC §6.3, §7.6)."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import methods
from .manifest import find_manifest
from .methods.base import Ctx
from .state import base_id
from .util import data_dir, read_json, write_json

CACHE_TTL = 3600  # SPEC said 24h; 1h because deleted folders must be noticed


def _step_from_record(ctx: Ctx, key: str, rec: Dict) -> Optional[Dict]:
    iid = base_id(key)
    item = ctx.catalog.get(iid)
    if not item:
        return None
    entry = item.platform_entry(ctx.plat)
    if not entry:
        return None
    snap = rec.get("step") or {}
    step = {"id": iid, "name": item.name, "scope": item.scope, "method": entry["method"],
            "params": {k: v for k, v in entry.items() if k != "method"}, "version": snap.get("version") or rec.get("version"),
            "smoke_test": item.smoke_test, "depends_on": item.deps_for(ctx.plat)}
    for k in ("packages", "python_version", "node_version"):
        if snap.get(k):
            step[k] = snap[k]
    return step


def relevant_records(ctx: Ctx) -> List[Dict]:
    """Project records plus the global tools this project relies on."""
    out = [{"id": k, "scope": "project", "rec": rec} for k, rec in ctx.pstate.items.items()]
    for k, rec in ctx.gstate.items.items():
        if str(ctx.project) in rec.get("used_by", []) or not ctx.pstate.items:
            out.append({"id": k, "scope": "global", "rec": rec})
    return out


def status(ctx: Ctx, all_global: bool = False) -> Dict:
    rows = []
    records = relevant_records(ctx)
    if all_global:
        ids = {r["id"] for r in records if r["scope"] == "global"}
        records += [{"id": i, "scope": "global", "rec": r} for i, r in ctx.gstate.items.items() if i not in ids]
    for r in records:
        step = _step_from_record(ctx, r["id"], r["rec"])
        healthy = None
        if step:
            healthy = methods.get(step["method"]).check(ctx, step).satisfies
        rec = r["rec"]
        rows.append({"id": r["id"], "name": step["name"] if step else r["id"], "scope": r["scope"],
                     "version": rec.get("version"), "healthy": healthy,
                     "installed_by": "env-wizard" if rec.get("installed_by_plugin") else
                                     ("you (with env-wizard's help)" if rec.get("via_handoff") else "already there"),
                     "used_by_projects": len(rec.get("used_by", [])) or None})
    manifest = find_manifest(ctx.project)
    return {"project": str(ctx.project), "items": rows, "manifest": str(manifest) if manifest else None,
            "unhealthy": [r["id"] for r in rows if r["healthy"] is False]}


def broken_items(ctx: Ctx) -> List[Dict]:
    out = []
    for r in relevant_records(ctx):
        step = _step_from_record(ctx, r["id"], r["rec"])
        if step and not methods.get(step["method"]).check(ctx, step).satisfies:
            out.append({"id": r["id"], "name": step["name"], "step": step, "rec": r["rec"]})
    return out


def _cache_key(project: Path) -> str:
    parts = [str(project)]
    for p in (project / ".env-wizard" / "state.json", project / "env-wizard.yaml", data_dir() / "state.json",
              project / ".venv", project / "node_modules"):
        try:
            parts.append(f"{p}:{p.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{p}:-")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def healthcheck(ctx: Ctx, use_cache: bool = True) -> Dict:
    project = ctx.project
    cache_path = data_dir() / "cache" / f"health-{hashlib.sha256(str(project).encode()).hexdigest()[:16]}.json"
    key = _cache_key(project)
    if use_cache:
        cached = read_json(cache_path, None)
        if cached and cached.get("key") == key and time.time() - cached.get("time", 0) < CACHE_TTL:
            return cached["result"]
    result: Dict = {"ok": True, "message": None}
    has_state = bool(ctx.pstate.items)
    manifest = find_manifest(project)
    if manifest and not has_state:
        result = {"ok": False, "kind": "manifest_not_applied",
                  "message": "This project has a setup file (env-wizard.yaml) that hasn't been applied yet. "
                             "Run /env-wizard apply to set it up."}
    elif has_state:
        broken = broken_items(ctx)
        if broken:
            shown = [b["name"] for b in broken[:3]]
            names = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
            if len(broken) > 3:
                names = ", ".join(shown) + " and more"
            result = {"ok": False, "kind": "broken", "items": [b["id"] for b in broken],
                      "message": f"{names} for this project {'is' if len(broken) == 1 else 'are'} missing or not "
                                 "working. Run /env-wizard repair."}
    write_json(cache_path, {"key": key, "time": time.time(), "result": result})
    return result


def hook_output(result: Dict) -> Optional[Dict]:
    """SessionStart hook JSON: silent when healthy, one line when not."""
    if result.get("ok"):
        return None
    msg = "env-wizard: " + result["message"]
    return {"systemMessage": msg,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg +
                                   " (Offer to run /env-wizard repair; don't install anything without the user's OK.)"}}

"""Executes an approved plan: idempotent steps, continue-on-failure, verify, record (SPEC §6.1, §8.2)."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import methods
from .detect import disk_free_mb, network_status
from .methods import shellinit
from .methods.base import Ctx, Outcome
from .methods.system import admin_handoff_brew, admin_handoff_winget
from .state import state_key
from .util import Lock, Log, data_dir, emit, new_id, now_iso, read_json, write_json

DIAGNOSES = [
    ("proxy", r"certificate verify failed|self[- ]signed|UNABLE_TO_GET_ISSUER_CERT|CERT_|SSL routines|invalid peer certificate",
     "A security certificate couldn't be checked. This usually means a company network or proxy is inspecting "
     "traffic. Ask IT whether developer downloads are allowed, or try from another network."),
    ("network", r"Could not resolve host|Failed to connect|Connection (refused|reset|timed out)|Temporary failure in "
                r"name resolution|getaddrinfo|ENOTFOUND|ETIMEDOUT|Network is unreachable|timed out after",
     "The download didn't get through. Check the internet connection and try again."),
    ("disk", r"No space left|ENOSPC|disk full|not enough space|There is not enough space",
     "The disk is full. Free up some space and try again."),
    ("permission", r"Permission denied|EACCES|EROFS|read-only file system|Access is denied|Operation not permitted|requires elevation|0x80070005",
     "The computer blocked the change because it needs extra permission. This often happens on work laptops; "
     "IT may need to help."),
    ("checksum", r"checksum|hash mismatch|integrity check|EINTEGRITY|doesn't match the expected hash",
     "A download was damaged or changed on the way. Retrying usually fixes this."),
    ("not_found", r"No package found|No matching distribution|not found in registry|E404|No solution found|"
                  r"No installed package found|could not find",
     "The requested version couldn't be found. The catalog may need an update."),
]


def diagnose(text: str) -> Dict:
    for kind, pat, msg in DIAGNOSES:
        if re.search(pat, text or "", re.I):
            return {"kind": kind, "message": msg}
    return {"kind": "unknown", "message": "Something went wrong. The details are saved in the log."}


def runs_dir() -> Path:
    return data_dir() / "runs"


def logs_dir() -> Path:
    return data_dir() / "logs"


def _latest_unfinished(plan_id: str) -> Optional[Dict]:
    best = None
    for p in sorted(runs_dir().glob("*.json")):
        r = read_json(p, None)
        if r and r.get("plan_id") == plan_id and r.get("status") != "done":
            best = r
    return best


def step_snapshot(step: Dict) -> Dict:
    """What we need later to re-check or repair this item without the original plan."""
    keys = ("version", "params", "packages", "python_version", "node_version", "smoke_test", "method", "name")
    return {k: step[k] for k in keys if step.get(k) is not None}


def _facts(results: Dict[str, Dict]) -> Dict:
    facts = {}
    for iid, label in (("python-env", "Python"), ("python", "Python"), ("node", "Node.js")):
        v = (results.get(iid) or {}).get("version")
        if v and label not in facts:
            facts[label] = v
    return facts


def apply_plan(plan: Dict, ctx: Ctx, resume: bool = False, allow: Optional[Dict] = None,
               check_network: bool = True) -> Dict:
    allow = allow or {}
    with Lock(data_dir() / "lock"):
        prev = _latest_unfinished(plan["plan_id"]) if resume else None
        run_id = prev["run_id"] if prev else new_id("run")
        ctx.run_id = run_id
        ctx.plan = dict(plan)
        run_path = runs_dir() / f"{run_id}.json"
        run = prev or {"run_id": run_id, "plan_id": plan["plan_id"], "project": plan["project"],
                       "started": now_iso(), "status": "running", "results": {},
                       "log": str(logs_dir() / f"{run_id}.log")}
        run["status"] = "running"
        ctx.runner.log = Log(Path(run["log"]))
        write_json(run_path, run)

        problems = _recheck_preflight(plan, ctx, check_network)
        if problems:
            run["status"] = "stopped"
            run["preflight"] = problems
            write_json(run_path, run)
            return {"run_id": run_id, "status": "stopped", "preflight": problems,
                    "message": "Nothing was changed."}

        results: Dict[str, Dict] = run["results"]
        restart_needed = False
        for step in plan["steps"]:
            iid = step["id"]
            if results.get(iid, {}).get("status") in ("ok", "skipped"):
                continue  # resumed run: already done
            t0 = time.time()
            res = _run_step(step, ctx, results, allow)
            res["seconds"] = round(time.time() - t0, 1)
            results[iid] = res
            restart_needed = restart_needed or bool(res.get("profile_edits_made"))
            write_json(run_path, run)
            emit({"event": "step", "id": iid, "name": step["name"], "status": res["status"],
                  "detail": res.get("detail", ""), "seconds": res["seconds"]})

        statuses = [r["status"] for r in results.values()]
        done = all(s in ("ok", "skipped", "unsupported") for s in statuses)
        run["status"] = "done" if done else "partial"
        run["finished"] = now_iso()
        run["restart_needed"] = restart_needed
        write_json(run_path, run)
        return {
            "run_id": run_id, "status": run["status"], "results": results,
            "restart_needed": restart_needed,
            "waiting_on_you": [i for i, r in results.items() if r["status"] in ("needs_admin", "needs_user")],
            "failed": [i for i, r in results.items() if r["status"] in ("failed", "test_failed")],
            "try_prompts": plan.get("try_prompts", []),
            "log": run["log"],
        }


def _recheck_preflight(plan: Dict, ctx: Ctx, check_network: bool) -> List[Dict]:
    problems = [p for p in plan.get("preflight", {}).get("problems", []) if p["kind"] == "unsupported_os"]
    need = int(plan["totals"]["download_mb"] * 1.2) + 100 if plan["totals"]["download_mb"] else 0
    free = min(disk_free_mb(ctx.project), disk_free_mb(data_dir()))
    if need and free < need:
        problems.append({"kind": "low_disk", "message": f"Needs about {need} MB free, but only {free} MB is available.",
                         "need_mb": need, "free_mb": free})
    if check_network and any(s["action"] == "install" for s in plan["steps"]):
        if network_status(timeout=4)["online"] is False:
            problems.append({"kind": "offline", "message": "This computer can't reach the internet right now."})
    return problems


def _state(ctx: Ctx, step: Dict):
    return ctx.pstate if step.get("scope") == "project" else ctx.gstate


def _run_step(step: Dict, ctx: Ctx, results: Dict[str, Dict], allow: Dict) -> Dict:
    if step["action"] in ("unsupported", "blocked"):
        return {"status": step["action"], "detail": step.get("reason", "")}
    waiting = [d for d in step.get("depends_on", [])
               if results.get(d, {}).get("status") not in (None, "ok", "skipped")]
    if waiting:
        return {"status": "blocked", "detail": "Waiting for " + ", ".join(waiting), "blocked_by": waiting}

    method = methods.get(step["method"])
    if step["id"] == "claude-md":
        ctx.plan["facts"] = _facts(results)
    if step["id"] == "python-env" and allow.get("recreate_venv"):
        step = dict(step, allow_recreate=True)

    chk = method.check(ctx, step)
    state = _state(ctx, step)
    key = state_key(step["id"], step.get("version"))
    rec = state.get(key)
    if chk.satisfies:
        if not rec:
            state.record(key, {"id": step["id"], "version": chk.version, "path": chk.path,
                                      "installed_by_plugin": False, "pre_existing": step["action"] == "reuse",
                                      "via_handoff": step["action"] == "handoff",
                                      "run_id": ctx.run_id, "step": step_snapshot(step)})
        _mark_used_by(ctx, step)
        return {"status": "skipped", "detail": "Already set up", "version": chk.version}

    if step["action"] == "handoff":
        if step["method"] in ("handoff-check", "check-only"):
            out = method.install(ctx, step)
        else:
            builder = admin_handoff_winget if step["method"] == "winget" else admin_handoff_brew
            out = Outcome("needs_admin", detail="Needs your password", handoff=builder(step))
        return {"status": out.status, "detail": out.detail, "handoff": out.handoff}

    out = method.install(ctx, step)
    if out.status != "ok":
        res = {"status": out.status, "detail": out.detail}
        if out.status == "failed":
            res["diagnosis"] = diagnose(out.error_text)
        if out.details:
            res["details"] = out.details
        return res

    edits = list(out.profile_edits)
    if step.get("shell_init"):
        edits += shellinit.apply_snippet(ctx, step["shell_init"])
    state.record(key, {
        "id": step["id"], "version": out.version or step.get("version"), "path": out.path,
        "method": step["method"], "installed_by_plugin": True, "pre_existing": False, "run_id": ctx.run_id,
        "paths_created": out.paths_created, "profile_edits": edits, "config_backups": out.config_backups,
        "details": out.details, "step": step_snapshot(step),
    })
    _mark_used_by(ctx, step)
    res = {"status": "ok", "detail": out.detail, "version": out.version, "profile_edits_made": bool(edits)}
    if out.details.get("skipped_scripts"):
        res["skipped_scripts"] = out.details["skipped_scripts"]
    smoke = method.smoke(ctx, step)
    if smoke is not None:
        res["test"] = {"status": smoke.status, "detail": smoke.detail}
        if smoke.status != "ok":
            res["status"] = "test_failed"
            res["diagnosis"] = diagnose(smoke.error_text)
    return res


def _mark_used_by(ctx: Ctx, step: Dict) -> None:
    """Remember which projects rely on each global tool, so undo never breaks another project."""
    if step.get("scope") != "project":
        return
    versions = {s["id"]: s.get("version") for s in (ctx.plan or {}).get("steps", [])}
    seen, queue = set(), list(step.get("depends_on", []))
    while queue:
        dep = queue.pop()
        if dep in seen:
            continue
        seen.add(dep)
        rec = ctx.gstate.get(state_key(dep, versions.get(dep)))
        if rec is not None:
            used = rec.setdefault("used_by", [])
            if str(ctx.project) not in used:
                used.append(str(ctx.project))
        item = ctx.catalog.get(dep)
        if item:
            queue += item.deps_for(ctx.plat)
    ctx.gstate.save()

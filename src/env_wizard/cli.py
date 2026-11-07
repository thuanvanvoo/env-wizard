"""Command-line entry point. All output is JSON for Claude to explain in plain language."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import List, Optional

from . import health, overlays, resolver, runner, undo
from .catalog import CatalogError, load_catalog
from .detect import detect_all, detect_platform, tool_env
from .manifest import ManifestError, find_manifest, parse_manifest
from .methods import shellinit
from .methods.base import Ctx, local_bin
from .methods.node import run_install_scripts
from .state import global_state, import_bootstrap, project_state
from .util import DataDirError, Lock, LockError, Log, Runner, data_dir, emit, project_dir, read_json, redact, home, write_json


def make_ctx(args, det=None, log: Optional[Log] = None) -> Ctx:
    proj = project_dir(getattr(args, "project", None))
    det = det or {"platform": detect_platform(), "admin": {}}
    gstate = global_state()
    env = tool_env(det.get("proxy", {})) if det.get("proxy") else {}
    ctx = Ctx(plat=det["platform"]["os"], project=proj, runner=Runner(log, env), catalog=load_catalog(),
              admin=det.get("admin", {}), gstate=gstate, pstate=project_state(proj))
    boot = import_bootstrap(gstate)
    if boot and (boot.get("items") or {}).get("uv", {}).get("needs_path_block"):
        # The bootstrap installed uv without touching PATH; add our own removable PATH entry now.
        edits = shellinit.apply_snippet(ctx, "local-bin", win_dir=str(local_bin()))
        gstate.record("uv", {"profile_edits": edits})
    return ctx


def _parse_items(values: List[str]):
    out = []
    for v in values or []:
        if "@" in v and not v.startswith("@"):
            iid, ver = v.rsplit("@", 1)
            out.append((iid, ver))
        else:
            out.append((v, None))
    return out


def cmd_detect(args):
    proj = project_dir(args.project)
    det = detect_all(proj, network=not args.no_network)
    det["project"] = str(proj)
    det["manifest"] = str(find_manifest(proj) or "") or None
    emit(det)


def cmd_catalog(args):
    cat = load_catalog()
    if args.match:
        emit({"profiles": cat.match_profile(args.match), "suggestions": cat.suggest(args.match)})
        return
    emit({
        "profiles": {p.id: {"name": p.name, "summary": p.summary, "items": p.items, "packages": p.packages}
                     for p in cat.profiles.values()},
        "items": {i.id: {"name": i.name, "summary": i.summary, "category": i.category, "optional": i.optional,
                         "platforms": sorted(i.platforms), "source": i.source} for i in cat.items.values()},
        "libraries": {eco: {n: {"version": p.version, "summary": p.summary} for n, p in pk.items()}
                      for eco, pk in cat.packages.items()},
        "permission_presets": sorted(cat.permission_presets), "mcp_servers": sorted(cat.mcp_servers),
    })


def cmd_validate(args):
    cat = load_catalog()
    path = Path(args.path) if args.path else find_manifest(project_dir(args.project))
    if not path:
        emit({"ok": False, "errors": ["No env-wizard.yaml found in this project."]})
        return 2
    try:
        m = parse_manifest(path, cat)
        emit({"ok": True, "manifest": m})
    except ManifestError as e:
        emit({"ok": False, "errors": e.errors})
        return 2


def _request_from_args(args, cat) -> resolver.Request:
    pk = {"python": [], "npm": []}
    for spec in args.package or []:
        eco, _, name = spec.partition(":")
        if eco not in pk or not name:
            raise SystemExit(f"--package must look like python:pandas or npm:serve (got {spec})")
        pk[eco].append(name.lower())
    manifest = None
    if args.manifest is not None:
        path = Path(args.manifest) if args.manifest else find_manifest(project_dir(args.project))
        if not path:
            raise ManifestError(["No env-wizard.yaml found in this project."])
        manifest = parse_manifest(path, cat)
    return resolver.Request(profiles=args.profile or [], items=_parse_items(args.item), packages=pk,
                            manifest=manifest, project_deps=args.project_deps, source=args.source)


def cmd_plan(args):
    proj = project_dir(args.project)
    det = detect_all(proj, network=not args.no_network)
    ctx = make_ctx(args, det)
    req = _request_from_args(args, ctx.catalog)
    plan = resolver.build_plan(ctx.catalog, det, ctx, req)
    resolver.save_plan(plan)
    emit(resolver.plan_view(plan) if not args.full else plan)


def cmd_apply(args):
    plan = resolver.load_plan(args.plan)
    proj = Path(plan["project"])
    det = detect_all(proj, network=False)
    args.project = str(proj)
    ctx = make_ctx(args, det)
    allow = {"recreate_venv": args.recreate_venv}
    summary = runner.apply_plan(plan, ctx, resume=args.resume, allow=allow, check_network=not args.no_network)
    emit({"event": "summary", **summary})
    return 0 if summary["status"] in ("done",) else 4


def cmd_status(args):
    ctx = make_ctx(args)
    emit(health.status(ctx, all_global=args.all))


def cmd_repair(args):
    proj = project_dir(args.project)
    det = detect_all(proj, network=not args.no_network)
    ctx = make_ctx(args, det)
    broken = health.broken_items(ctx)
    manifest = find_manifest(proj)
    if not broken and not (manifest and not ctx.pstate.items):
        emit({"ok": True, "message": "Everything is working. Nothing to repair."})
        return
    items, pk = [], {"python": [], "npm": []}
    for b in broken:
        if b["id"] == "python-packages":
            pk["python"] += [p["name"].lower() for p in b["step"].get("packages", [])]
        elif b["id"] == "npm-packages":
            pk["npm"] += [p["name"].lower() for p in b["step"].get("packages", [])]
        else:
            items.append((b["id"], b["step"].get("version")))
    req = resolver.Request(items=items, packages=pk, source="repair",
                           manifest=parse_manifest(manifest, ctx.catalog) if manifest and not ctx.pstate.items else None)
    plan = resolver.build_plan(ctx.catalog, det, ctx, req)
    resolver.save_plan(plan)
    emit({"broken": [b["id"] for b in broken], "plan": resolver.plan_view(plan)})


def cmd_healthcheck(args):
    try:
        ctx = make_ctx(args)
        if not ctx.pstate.items and not find_manifest(ctx.project):
            return 0
        result = health.healthcheck(ctx, use_cache=not args.no_cache)
    except Exception:  # the hook must never get in the user's way
        return 0
    if args.hook:
        out = health.hook_output(result)
        if out:
            print(json.dumps(out))
    else:
        emit(result)
    return 0


def cmd_undo(args):
    ctx = make_ctx(args)
    target = args.target or "all"
    plan = undo.plan_undo(ctx, target, include_shared=args.include_shared)
    if not args.confirm:
        emit({"preview": True, **undo.preview(plan),
              "next": "Show this to the user. Only after they agree, run again with --confirm."})
        return
    with Lock(data_dir() / "lock"):
        ctx.runner.log = Log(data_dir() / "logs" / f"undo-{target.replace('/', '_')}.log")
        emit(undo.execute(ctx, plan))


def cmd_overlay(args):
    r = Runner(Log(data_dir() / "logs" / "overlays.log"))
    try:
        if args.action == "add":
            emit(overlays.add(args.source, r, name=args.name))
        elif args.action == "update":
            emit(overlays.update(args.name, r))
        elif args.action == "approve":
            emit(overlays.approve(args.name))
        elif args.action == "remove":
            emit(overlays.remove(args.name))
        else:
            emit(overlays.list_overlays())
    except overlays.OverlayError as e:
        emit({"ok": False, "error": str(e)})
        return 2


def cmd_allow_scripts(args):
    ctx = make_ctx(args)
    out = run_install_scripts(ctx, args.packages, args.node_version)
    emit(out.to_dict())


def cmd_logs(args):
    runs = sorted((data_dir() / "runs").glob("*.json"))
    if not args.report:
        emit({"runs": [{"run_id": r.stem, "status": (read_json(r, {}) or {}).get("status"),
                        "log": (read_json(r, {}) or {}).get("log")} for r in runs[-10:]]})
        return
    run = read_json(data_dir() / "runs" / f"{args.report}.json", None)
    if not run:
        emit({"ok": False, "error": f"No run called {args.report}."})
        return 2
    log_text = ""
    try:
        log_text = Path(run["log"]).read_text(encoding="utf-8", errors="replace")[-20000:]
    except OSError:
        pass
    report = {"run": {k: v for k, v in run.items() if k != "log"}, "platform": detect_platform(), "log_tail": log_text}
    text = redact(json.dumps(report, indent=2)).replace(str(home()), "~")
    out = data_dir() / "reports" / f"{args.report}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    emit({"report": str(out), "note": "Secrets and your home folder path were removed. Check it before sharing."})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="env-wizard")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, network=True):
        sp.add_argument("--project", help="project folder (default: current folder)")
        if network:
            sp.add_argument("--no-network", action="store_true", help="skip the internet check")
        return sp

    common(sub.add_parser("detect")).set_defaults(fn=cmd_detect)
    c = sub.add_parser("catalog")
    c.add_argument("--match", help="free text to match against profiles and items")
    c.set_defaults(fn=cmd_catalog)
    v = common(sub.add_parser("validate"), network=False)
    v.add_argument("path", nargs="?")
    v.set_defaults(fn=cmd_validate)

    pl = common(sub.add_parser("plan"))
    pl.add_argument("--profile", action="append")
    pl.add_argument("--item", action="append", help="catalog id, optionally id@version")
    pl.add_argument("--package", action="append", help="python:NAME or npm:NAME")
    pl.add_argument("--manifest", nargs="?", const="", default=None, help="use env-wizard.yaml (optional path)")
    pl.add_argument("--project-deps", action="store_true", help="install requirements the project lists")
    pl.add_argument("--source", default="chat")
    pl.add_argument("--full", action="store_true")
    pl.set_defaults(fn=cmd_plan)

    ap = sub.add_parser("apply")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--recreate-venv", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.set_defaults(fn=cmd_apply)

    st = common(sub.add_parser("status"), network=False)
    st.add_argument("--all", action="store_true")
    st.set_defaults(fn=cmd_status)
    common(sub.add_parser("repair")).set_defaults(fn=cmd_repair)
    hc = common(sub.add_parser("healthcheck"), network=False)
    hc.add_argument("--hook", action="store_true")
    hc.add_argument("--no-cache", action="store_true")
    hc.set_defaults(fn=cmd_healthcheck)

    un = common(sub.add_parser("undo"), network=False)
    un.add_argument("target", nargs="?", help="'all', an item id, or a run id")
    un.add_argument("--confirm", action="store_true")
    un.add_argument("--include-shared", action="store_true")
    un.set_defaults(fn=cmd_undo)

    ov = sub.add_parser("overlay")
    ov.add_argument("action", choices=["add", "update", "approve", "remove", "list"])
    ov.add_argument("source_or_name", nargs="?")
    ov.add_argument("--name")
    ov.set_defaults(fn=cmd_overlay)

    sc = common(sub.add_parser("allow-scripts"), network=False)
    sc.add_argument("packages", nargs="+")
    sc.add_argument("--node-version")
    sc.set_defaults(fn=cmd_allow_scripts)

    lg = sub.add_parser("logs")
    lg.add_argument("--report", metavar="RUN_ID")
    lg.set_defaults(fn=cmd_logs)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "overlay":
        if args.action == "add":
            args.source = args.source_or_name
        elif args.action != "list":
            args.name = args.name or args.source_or_name
    try:
        return args.fn(args) or 0
    except (ManifestError,) as e:
        emit({"ok": False, "errors": e.errors})
        return 2
    except (CatalogError, resolver.PlanError, LockError) as e:
        emit({"ok": False, "error": str(e)})
        return 2
    except KeyboardInterrupt:
        emit({"ok": False, "error": "Stopped. Run the same command with --resume to continue where it left off."})
        return 130
    except DataDirError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    except Exception as e:  # last resort: never dump a traceback on a non-technical user
        try:
            path = data_dir() / "logs" / "crash.log"
        except DataDirError:
            path = Path(tempfile.gettempdir()) / "env-wizard-crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact(traceback.format_exc()), encoding="utf-8")
        emit({"ok": False, "error": f"env-wizard hit an unexpected problem ({type(e).__name__}). "
                                    f"Details were saved to {path}."})
        return 1


if __name__ == "__main__":
    sys.exit(main())

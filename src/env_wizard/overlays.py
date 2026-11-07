"""Org catalog overlays: git repo (or local folder), pinned to a commit, approved by the user (SPEC §5.5)."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Dict, Optional

from . import yamlish
from .catalog import Catalog, CatalogError, load_catalog, overlay_registry_path
from .util import Runner, data_dir, now_iso, plugin_root, read_json, which, write_json


class OverlayError(ValueError):
    pass


def _registry() -> Dict:
    return read_json(overlay_registry_path(), {}) or {"overlays": {}}


def _save(reg: Dict) -> None:
    write_json(overlay_registry_path(), reg)


def _name_from(source: str) -> str:
    base = re.sub(r"\.git$", "", source.rstrip("/").split("/")[-1].split(":")[-1])
    return re.sub(r"[^a-z0-9\-]", "-", base.lower()).strip("-") or "overlay"


def _dir_hash(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            h.update(str(p.relative_to(path)).encode())
            h.update(p.read_bytes())
    return "content-" + h.hexdigest()[:12]


def _fetch(source: str, dest: Path, runner: Runner) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    local = Path(source).expanduser()
    if local.is_dir():
        shutil.copytree(local, dest, ignore=shutil.ignore_patterns(".git"))
        git = which("git")
        if git and (local / ".git").exists():
            res = runner.run([git, "-C", str(local), "rev-parse", "HEAD"], timeout=30)
            if res.ok:
                return res.out.strip()
        return _dir_hash(dest)
    git = which("git")
    if not git:
        raise OverlayError("Git is needed to download your organization's catalog. Set up Git first.")
    res = runner.run([git, "clone", "--depth", "1", "--quiet", source, str(dest)], timeout=600,
                     env={"GIT_TERMINAL_PROMPT": "0"})
    if not res.ok:
        raise OverlayError(f"Couldn't download {source}. If this is a private repository, ask your IT team for access. "
                           f"(git said: {res.text[-300:]})")
    commit = runner.run([git, "-C", str(dest), "rev-parse", "HEAD"], timeout=30).out.strip()
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return commit


def _summarize(path: Path, name: str) -> Dict:
    cat = Catalog()
    try:
        cat.load_dir(path, source=f"overlay:{name}")
    except (CatalogError, yamlish.YamlError) as e:
        raise OverlayError(f"This catalog has a problem and can't be used: {e}")
    for iid, item in cat.items.items():
        if not iid.startswith(f"{name}/"):
            raise OverlayError(f"Item '{iid}' must be named '{name}/...' (overlay items are namespaced).")
    return {"items": {i: {"name": it.name, "summary": it.summary,
                         "methods": sorted({e["method"] for e in it.platforms.values()})} for i, it in cat.items.items()},
            "profiles": {p: pr.name for p, pr in cat.profiles.items()},
            "package_pins": {eco: {n: p.version for n, p in pk.items()} for eco, pk in cat.packages.items() if pk}}


def _diff(old: Dict, new: Dict) -> Dict:
    out = {}
    for key in ("items", "profiles"):
        o, n = old.get(key, {}), new.get(key, {})
        out[key] = {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)),
                    "changed": sorted(k for k in set(o) & set(n) if o[k] != n[k])}
    return out


def add(source: str, runner: Runner, name: Optional[str] = None) -> Dict:
    name = name or _name_from(source)
    reg = _registry()
    if name in reg["overlays"] and reg["overlays"][name].get("status") == "approved":
        raise OverlayError(f"An overlay called '{name}' is already added. Use 'overlay update {name}'.")
    pending = data_dir() / "overlays" / f"{name}.pending"
    commit = _fetch(source, pending, runner)
    summary = _summarize(pending, name)
    reg["overlays"][name] = {"source": source, "status": "pending", "pending_commit": commit,
                             "path": str(data_dir() / "overlays" / name), "requested": now_iso(),
                             "pending_summary": summary}
    _save(reg)
    return {"name": name, "source": source, "commit": commit, "summary": summary, "needs_approval": True}


def update(name: str, runner: Runner) -> Dict:
    reg = _registry()
    entry = reg["overlays"].get(name)
    if not entry:
        raise OverlayError(f"No overlay called '{name}'.")
    pending = data_dir() / "overlays" / f"{name}.pending"
    commit = _fetch(entry["source"], pending, runner)
    if commit == entry.get("commit"):
        shutil.rmtree(pending, ignore_errors=True)
        return {"name": name, "up_to_date": True, "commit": commit}
    summary = _summarize(pending, name)
    entry.update(pending_commit=commit, pending_summary=summary, requested=now_iso())
    _save(reg)
    return {"name": name, "commit": commit, "previous_commit": entry.get("commit"),
            "changes": _diff(entry.get("summary", {}), summary), "needs_approval": True}


def approve(name: str) -> Dict:
    reg = _registry()
    entry = reg["overlays"].get(name)
    if not entry or not entry.get("pending_commit"):
        raise OverlayError(f"Nothing is waiting for approval for '{name}'.")
    pending = data_dir() / "overlays" / f"{name}.pending"
    final = Path(entry["path"])
    if final.exists():
        shutil.rmtree(final)
    shutil.move(str(pending), str(final))
    entry.update(status="approved", commit=entry.pop("pending_commit"), summary=entry.pop("pending_summary"),
                 approved=now_iso())
    _save(reg)
    load_catalog()  # make sure the combined catalog is still valid
    return {"name": name, "commit": entry["commit"], "status": "approved"}


def remove(name: str) -> Dict:
    reg = _registry()
    entry = reg["overlays"].pop(name, None)
    if not entry:
        raise OverlayError(f"No overlay called '{name}'.")
    for p in (Path(entry["path"]), data_dir() / "overlays" / f"{name}.pending"):
        shutil.rmtree(p, ignore_errors=True)
    _save(reg)
    return {"name": name, "removed": True}


def list_overlays() -> Dict:
    reg = _registry()
    rows = [{"name": n, "source": e["source"], "status": e["status"], "commit": e.get("commit"),
             "pending_commit": e.get("pending_commit")} for n, e in sorted(reg["overlays"].items())]
    suggested = []
    cfg = plugin_root() / "config" / "default-overlays.yaml"
    if cfg.is_file():
        for o in (yamlish.load_file(cfg) or {}).get("overlays") or []:
            if o.get("name") not in reg["overlays"]:
                suggested.append(o)
    return {"overlays": rows, "suggested_by_your_organization": suggested}

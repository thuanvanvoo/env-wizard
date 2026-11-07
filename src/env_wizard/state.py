"""Install records. Undo relies only on what is written here (SPEC §5.4)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .util import data_dir, now_iso, read_json, write_json

SCHEMA = 1
# Runtimes can exist in several versions side by side; each version gets its own record.
VERSIONED = {"python", "node"}


def state_key(iid: str, version: Optional[str]) -> str:
    return f"{iid}@{version}" if iid in VERSIONED and version else iid


def base_id(key: str) -> str:
    return key.split("@", 1)[0]


class State:
    def __init__(self, path: Path, scope: str):
        self.path = path
        self.scope = scope
        raw = read_json(path, None) or {}
        self.data = {"schema": raw.get("schema", SCHEMA), "items": raw.get("items", {})}

    @property
    def items(self) -> Dict[str, Dict]:
        return self.data["items"]

    def get(self, iid: str) -> Optional[Dict]:
        return self.items.get(iid)

    def record(self, iid: str, rec: Dict) -> None:
        prev = self.items.get(iid) or {}
        merged = dict(prev)
        merged.update(rec)
        # Never forget that something pre-existed, and never lose earlier created paths/edits.
        merged["pre_existing"] = prev.get("pre_existing", rec.get("pre_existing", False))
        merged["installed_by_plugin"] = prev.get("installed_by_plugin", False) or rec.get("installed_by_plugin", False)
        for key in ("paths_created", "profile_edits", "config_backups"):
            merged[key] = _merge_list(prev.get(key), rec.get(key))
        merged.setdefault("details", {})
        if prev.get("details") and rec.get("details"):
            d = dict(prev["details"])
            for k, v in rec["details"].items():
                if isinstance(v, list) and isinstance(d.get(k), list):
                    d[k] = _merge_list(d[k], v)
                else:
                    d[k] = v
            merged["details"] = d
        merged["scope"] = self.scope
        merged["timestamp"] = now_iso()
        self.items[iid] = merged
        self.save()

    def remove(self, iid: str) -> None:
        self.items.pop(iid, None)
        self.save()

    def save(self) -> None:
        write_json(self.path, self.data)


def _merge_list(a, b) -> List:
    out = list(a or [])
    for x in b or []:
        if x not in out:
            out.append(x)
    return out


def global_state() -> State:
    return State(data_dir() / "state.json", "global")


def project_state(project: Path) -> State:
    return State(project / ".env-wizard" / "state.json", "project")


def state_for(scope: str, project: Path) -> State:
    return project_state(project) if scope == "project" else global_state()


def import_bootstrap(gstate: State) -> Optional[Dict]:
    """Record uv/Python installed by the bootstrap shim (before the engine could run)."""
    path = data_dir() / "bootstrap.json"
    boot = read_json(path, None)
    if not boot or boot.get("imported"):
        return None
    for iid, rec in (boot.get("items") or {}).items():
        key = state_key(iid, rec.get("version"))
        if not gstate.get(key):
            gstate.record(key, {"id": iid, "installed_by_plugin": True, "pre_existing": False,
                                "run_id": "bootstrap", **rec})
    boot["imported"] = True
    write_json(path, boot)
    return boot

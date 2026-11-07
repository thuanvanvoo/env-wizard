"""env-wizard.yaml manifests. Manifests can only reference catalog items (SPEC §5.2)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from . import yamlish
from .catalog import FORBIDDEN_KEYS

MANIFEST_NAME = "env-wizard.yaml"
TOP_KEYS = {"version", "name", "items", "profiles", "accounts", "project_deps", "description"}


class ManifestError(ValueError):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def find_manifest(project: Path) -> Optional[Path]:
    p = project / MANIFEST_NAME
    return p if p.is_file() else None


def parse_manifest(path: Path, catalog) -> Dict:
    """Returns {'name', 'profiles', 'items': [(id, version)], 'packages': {eco: [names]}, 'project_deps', 'notes'}."""
    try:
        data = yamlish.load_file(path)
    except yamlish.YamlError as e:
        raise ManifestError([f"{path.name} line {e.line}: {e.msg}" if e.line else str(e)])
    except OSError as e:
        raise ManifestError([f"Can't read {path}: {e}"])
    errors: List[str] = []
    notes: List[str] = []
    if not isinstance(data, dict):
        raise ManifestError([f"{path.name}: expected keys like 'version' and 'items' at the top level"])
    for k in data:
        if k.lower() in FORBIDDEN_KEYS:
            errors.append(f"'{k}' is not allowed. Setup files can only list tools from the env-wizard catalog, "
                          "never commands or download links.")
        elif k not in TOP_KEYS:
            errors.append(f"Unknown setting '{k}'. Allowed: {', '.join(sorted(TOP_KEYS))}.")
    if data.get("version") != 1:
        errors.append("'version: 1' is required on the first line.")
    out = {"name": data.get("name") or path.parent.name, "profiles": [], "items": [],
           "packages": {"python": [], "npm": []}, "project_deps": bool(data.get("project_deps", False)),
           "notes": notes, "path": str(path)}

    for p in data.get("profiles") or []:
        if not isinstance(p, str) or p not in catalog.profiles:
            errors.append(f"profiles: unknown profile {p!r}. Available: {', '.join(sorted(catalog.profiles))}.")
        else:
            out["profiles"].append(p)

    items = data.get("items") or []
    if not isinstance(items, list):
        errors.append("'items' must be a list (each line starting with '- ').")
        items = []
    for idx, entry in enumerate(items, start=1):
        where = f"items[{idx}]"
        if isinstance(entry, str):
            _add_item(out, catalog, entry, None, where, errors)
        elif isinstance(entry, dict) and len(entry) == 1:
            (key, val), = entry.items()
            if key.lower() in FORBIDDEN_KEYS:
                errors.append(f"{where}: '{key}' is not allowed. Setup files can only list catalog tools.")
            elif key.startswith("package:"):
                eco = key.split(":", 1)[1]
                if eco not in ("python", "npm"):
                    errors.append(f"{where}: unknown package type '{eco}' (use package:python or package:npm).")
                    continue
                names = val if isinstance(val, list) else [val]
                for n in names:
                    if not isinstance(n, str):
                        errors.append(f"{where}: package names must be text.")
                    elif n.lower() not in catalog.packages[eco]:
                        errors.append(f"{where}: '{n}' isn't on the approved {eco} library list.")
                    else:
                        out["packages"][eco].append(n.lower())
            elif key in ("mcp", "template", "cc-permissions"):
                _add_item(out, catalog, f"{key}:{val}", None, where, errors)
            else:
                if val is not None and not isinstance(val, (str, int)):
                    errors.append(f"{where}: version for '{key}' must be text like \"3.12\".")
                    continue
                _add_item(out, catalog, key, None if val is None else str(val), where, errors)
        else:
            errors.append(f"{where}: expected a tool name, or 'name: version'.")

    if data.get("accounts"):
        notes.append("Account sign-in (accounts:) arrives in a later version and was skipped.")
    if errors:
        raise ManifestError(errors)
    return out


def _add_item(out, catalog, iid, version, where, errors):
    item = catalog.get(iid)
    if not item:
        sugg = catalog.suggest(iid)
        hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
        errors.append(f"{where}: '{iid}' isn't in the env-wizard catalog.{hint}")
        return
    out["items"].append((iid, version))

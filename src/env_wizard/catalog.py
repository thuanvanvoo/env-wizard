"""Catalog loading and validation (core catalog + approved org overlays)."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import yamlish
from .util import data_dir, plugin_root, read_json

ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*(?::[a-z0-9][a-z0-9\-]*)?$")
OVERLAY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*/[a-z0-9][a-z0-9\-]*$")
CATEGORIES = {"manager", "runtime", "cli", "package", "project", "cc-config", "app", "account"}
PLATFORMS = {"macos", "windows", "linux", "wsl"}
# Methods are implemented in Python inside the plugin. Catalog/overlay entries can only pick one
# of these and pass parameters; they can never supply commands or scripts.
METHODS = {
    "handoff-check", "check-only", "uv-standalone", "uv-python", "uv-venv", "uv-tool",
    "python-packages", "project-python-deps", "fnm-script", "fnm-node", "npm-packages",
    "project-node-deps", "brew-formula", "brew-cask", "winget", "cc-permissions", "cc-mcp",
    "claude-md", "template",
}
FORBIDDEN_KEYS = {"run", "script", "scripts", "command", "commands", "shell", "exec", "url", "urls", "curl"}


class CatalogError(ValueError):
    pass


@dataclass
class Item:
    id: str
    name: str
    summary: str
    category: str
    platforms: Dict[str, Dict]
    default_version: Optional[str] = None
    allowed_versions: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)
    platform_depends_on: Dict[str, List[str]] = field(default_factory=dict)
    requires_admin: Dict[str, bool] = field(default_factory=dict)
    size_mb: int = 0
    est_minutes: int = 1
    scope: str = "global"
    smoke_test: Optional[Dict] = None
    user_space_alternative: Optional[str] = None
    license_note: Optional[str] = None
    optional: bool = False
    aliases: List[str] = field(default_factory=list)
    shell_init: Optional[str] = None
    source: str = "core"

    def deps_for(self, plat: str) -> List[str]:
        extra = self.platform_depends_on.get(plat)
        if extra is None and plat == "wsl":
            extra = self.platform_depends_on.get("linux")
        return list(self.depends_on) + list(extra or [])

    def platform_entry(self, plat: str) -> Optional[Dict]:
        if plat in self.platforms:
            return self.platforms[plat]
        if plat == "wsl" and "linux" in self.platforms:
            return self.platforms["linux"]
        return None


@dataclass
class Package:
    ecosystem: str  # python | npm
    name: str
    version: str
    summary: str
    import_name: Optional[str] = None


@dataclass
class Profile:
    id: str
    name: str
    summary: str
    items: List[str]
    packages: Dict[str, List[str]] = field(default_factory=dict)
    try_prompts: List[str] = field(default_factory=list)
    claude_md: str = ""
    keywords: List[str] = field(default_factory=list)
    source: str = "core"


def _req(d: dict, key: str, typ, where: str):
    if key not in d:
        raise CatalogError(f"{where}: missing required field '{key}'")
    if not isinstance(d[key], typ):
        raise CatalogError(f"{where}: field '{key}' has the wrong type")
    return d[key]


def _check_forbidden(obj, where: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                raise CatalogError(f"{where}: '{k}' is not allowed. Catalog entries can only use built-in install methods.")
            _check_forbidden(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _check_forbidden(v, where)


def parse_item(d: dict, where: str, source: str = "core") -> Item:
    if not isinstance(d, dict):
        raise CatalogError(f"{where}: expected a mapping")
    _check_forbidden(d, where)
    iid = _req(d, "id", str, where)
    id_re = OVERLAY_ID_RE if source != "core" else ID_RE
    if not id_re.match(iid):
        hint = " Overlay items must be namespaced like 'acme/tool'." if source != "core" else ""
        raise CatalogError(f"{where}: invalid id '{iid}'.{hint}")
    cat = _req(d, "category", str, where)
    if cat not in CATEGORIES:
        raise CatalogError(f"{where}: unknown category '{cat}'")
    plats = _req(d, "platforms", dict, where)
    for p, entry in plats.items():
        if p not in PLATFORMS:
            raise CatalogError(f"{where}: unknown platform '{p}'")
        if not isinstance(entry, dict) or entry.get("method") not in METHODS:
            raise CatalogError(f"{where}: platform '{p}' must name one of the built-in methods")
    for v in ("default_version", "allowed_versions"):
        if d.get(v) is not None and not isinstance(d[v], str):
            raise CatalogError(f"{where}: '{v}' must be a quoted string (e.g. \"3.12\")")
    return Item(
        id=iid,
        name=_req(d, "name", str, where),
        summary=_req(d, "summary", str, where),
        category=cat,
        platforms=plats,
        default_version=d.get("default_version"),
        allowed_versions=d.get("allowed_versions"),
        depends_on=list(d.get("depends_on") or []),
        platform_depends_on={k: list(v or []) for k, v in (d.get("platform_depends_on") or {}).items()},
        requires_admin=dict(d.get("requires_admin") or {}),
        size_mb=int(d.get("size_mb") or 0),
        est_minutes=int(d.get("est_minutes") or 1),
        scope=d.get("scope", "global"),
        smoke_test=d.get("smoke_test"),
        user_space_alternative=d.get("user_space_alternative"),
        license_note=d.get("license_note"),
        optional=bool(d.get("optional", False)),
        aliases=list(d.get("aliases") or []),
        shell_init=d.get("shell_init"),
        source=source,
    )


def parse_profile(d: dict, where: str, source: str = "core") -> Profile:
    _check_forbidden(d, where)
    return Profile(
        id=_req(d, "id", str, where),
        name=_req(d, "name", str, where),
        summary=_req(d, "summary", str, where),
        items=list(_req(d, "items", list, where)),
        packages={k: list(v or []) for k, v in (d.get("packages") or {}).items()},
        try_prompts=list(d.get("try_prompts") or []),
        claude_md=d.get("claude_md") or "",
        keywords=list(d.get("keywords") or []),
        source=source,
    )


class Catalog:
    def __init__(self):
        self.items: Dict[str, Item] = {}
        self.profiles: Dict[str, Profile] = {}
        self.packages: Dict[str, Dict[str, Package]] = {"python": {}, "npm": {}}
        self.permission_presets: Dict[str, List[str]] = {}
        self.mcp_servers: Dict[str, Dict] = {}
        self.order: List[str] = []

    # ---------------------------------------------------------- loading
    def load_dir(self, root: Path, source: str = "core") -> None:
        items_dir = root / "items"
        for path in sorted(items_dir.glob("*.yaml")) if items_dir.is_dir() else []:
            item = parse_item(yamlish.load_file(path), str(path.name), source)
            if item.id in self.items:
                raise CatalogError(f"{path.name}: duplicate item id '{item.id}'")
            self.items[item.id] = item
            self.order.append(item.id)
        prof_dir = root / "profiles"
        for path in sorted(prof_dir.glob("*.yaml")) if prof_dir.is_dir() else []:
            prof = parse_profile(yamlish.load_file(path), str(path.name), source)
            if source != "core" and not OVERLAY_ID_RE.match(prof.id):
                raise CatalogError(f"{path.name}: overlay profile ids must be namespaced like 'acme/onboarding'")
            self.profiles[prof.id] = prof
        pkg_dir = root / "packages"
        for eco in ("python", "npm"):
            path = pkg_dir / f"{eco}.yaml"
            if path.is_file():
                data = yamlish.load_file(path) or {}
                _check_forbidden(data, path.name)
                for name, meta in (data.get("packages") or {}).items():
                    if not isinstance(meta, dict) or not isinstance(meta.get("version"), str):
                        raise CatalogError(f"{path.name}: package '{name}' needs a quoted version")
                    self.packages[eco][name.lower()] = Package(eco, name, meta["version"], meta.get("summary", ""),
                                                               meta.get("import"))
        cc = root / "cc"
        if (cc / "permissions.yaml").is_file():
            data = yamlish.load_file(cc / "permissions.yaml") or {}
            for k, v in (data.get("presets") or {}).items():
                self.permission_presets[k] = list(v or [])
        if (cc / "mcp.yaml").is_file():
            data = yamlish.load_file(cc / "mcp.yaml") or {}
            for k, v in (data.get("servers") or {}).items():
                self.mcp_servers[k] = v

    def validate_references(self) -> None:
        for item in self.items.values():
            for dep in item.depends_on + [d for v in item.platform_depends_on.values() for d in v]:
                if dep not in self.items:
                    raise CatalogError(f"{item.id}: depends on unknown item '{dep}'")
            if item.user_space_alternative and item.user_space_alternative not in self.items:
                raise CatalogError(f"{item.id}: unknown user_space_alternative")
        for prof in self.profiles.values():
            for iid in prof.items:
                if not self.resolvable(iid):
                    raise CatalogError(f"profile {prof.id}: unknown item '{iid}'")
            for eco, names in prof.packages.items():
                for n in names:
                    if n.lower() not in self.packages.get(eco, {}):
                        raise CatalogError(f"profile {prof.id}: package '{n}' is not on the {eco} allow-list")

    # ---------------------------------------------------------- lookup
    def resolvable(self, iid: str) -> bool:
        if iid in self.items:
            return True
        if iid.startswith("cc-permissions:"):
            return iid.split(":", 1)[1] in self.permission_presets
        if iid.startswith("mcp:"):
            return iid.split(":", 1)[1] in self.mcp_servers
        if iid.startswith("template:"):
            return (plugin_root() / "templates" / iid.split(":", 1)[1]).is_dir()
        return False

    def get(self, iid: str) -> Optional[Item]:
        if iid in self.items:
            return self.items[iid]
        return self._virtual(iid)

    def _virtual(self, iid: str) -> Optional[Item]:
        """Items parameterised by name (permission presets, MCP servers, templates)."""
        if not self.resolvable(iid):
            return None
        kind, name = iid.split(":", 1)
        allp = lambda method, **kw: {p: dict(method=method, **kw) for p in PLATFORMS}
        if kind == "cc-permissions":
            return Item(iid, f"Claude Code permissions ({name})",
                        "Lets Claude run the tools above in this project without asking every time.",
                        "cc-config", allp("cc-permissions", preset=name), scope="project")
        if kind == "mcp":
            srv = self.mcp_servers[name]
            return Item(iid, srv.get("name", name), srv.get("summary", ""), "cc-config",
                        allp("cc-mcp", server=name), depends_on=list(srv.get("depends_on") or []), scope="project")
        if kind == "template":
            return Item(iid, f"Starter files ({name})", "A few example files to get you started.", "project",
                        allp("template", template=name), scope="project")
        return None

    def suggest(self, query: str, n: int = 3) -> List[str]:
        q = query.lower().strip()
        names = {}
        for item in self.items.values():
            for key in [item.id, item.name.lower(), *[a.lower() for a in item.aliases]]:
                names[key] = item.id
        for eco, pkgs in self.packages.items():
            for name in pkgs:
                names[name] = f"package:{eco}:{name}"
        hits = difflib.get_close_matches(q, list(names), n=n, cutoff=0.6)
        return list(dict.fromkeys(names[h] for h in hits))

    def match_profile(self, text: str) -> List[str]:
        words = set(re.findall(r"[a-z]+", text.lower()))
        scored = []
        for p in self.profiles.values():
            score = len(words & {k.lower() for k in p.keywords})
            if score:
                scored.append((score, p.id))
        return [pid for _, pid in sorted(scored, key=lambda x: (-x[0], x[1]))]


def overlay_registry_path() -> Path:
    return data_dir() / "overlays.json"


def load_catalog(include_overlays: bool = True) -> Catalog:
    cat = Catalog()
    cat.load_dir(plugin_root() / "catalog", "core")
    if include_overlays:
        reg = read_json(overlay_registry_path(), {}) or {}
        for name, entry in sorted(reg.get("overlays", {}).items()):
            if entry.get("status") != "approved":
                continue
            path = Path(entry["path"])
            if path.is_dir():
                cat.load_dir(path, source=f"overlay:{name}")
    cat.validate_references()
    return cat

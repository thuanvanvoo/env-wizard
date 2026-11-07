"""Claude Code project configuration: permissions, MCP servers, CLAUDE.md, starter templates.

All writes are project-scoped, merged non-destructively, and backed up first (SPEC §8.6).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Dict, List

from ..util import data_dir, file_sha, plugin_root
from .base import Check, Ctx, Method, Outcome

MD_START = "<!-- env-wizard:start -->"
MD_END = "<!-- env-wizard:end -->"


def backup(ctx: Ctx, path: Path) -> str:
    if not path.exists():
        return ""
    dest = data_dir() / "backups" / ctx.run_id / re.sub(r"[^A-Za-z0-9._-]", "_", str(path))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return str(dest)


def _load_json(path: Path):
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return None, "it isn't a JSON object"
        return data, None
    except ValueError as e:
        return None, f"it isn't valid JSON ({e})"


def _write_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class CcPermissions(Method):
    name = "cc-permissions"

    def _entries(self, ctx, step) -> List[str]:
        return ctx.catalog.permission_presets[step["params"]["preset"]]

    def _path(self, ctx) -> Path:
        return ctx.project / ".claude" / "settings.json"

    def check(self, ctx, step):
        data, err = _load_json(self._path(ctx))
        if err or data is None:
            return Check(False)
        allow = ((data.get("permissions") or {}).get("allow")) or []
        return Check(True, all(e in allow for e in self._entries(ctx, step)))

    def preview(self, ctx, step) -> Dict:
        data, _ = _load_json(self._path(ctx))
        allow = (((data or {}).get("permissions") or {}).get("allow")) or []
        return {"file": str(self._path(ctx)), "add_allow": [e for e in self._entries(ctx, step) if e not in allow]}

    def install(self, ctx, step):
        path = self._path(ctx)
        data, err = _load_json(path)
        if err:
            return Outcome("failed", detail=f"Didn't change {path.name} because {err}. Nothing was overwritten.")
        b = backup(ctx, path)
        perms = data.setdefault("permissions", {})
        allow = perms.setdefault("allow", [])
        added = [e for e in self._entries(ctx, step) if e not in allow]
        allow.extend(added)
        created = not path.exists()
        _write_json(path, data)
        return Outcome("ok", config_backups=[b] if b else [],
                       paths_created=[str(path)] if created else [],
                       details={"file": str(path), "added_allow": added})

    def uninstall(self, ctx, rec):
        path = Path(rec["details"]["file"])
        data, err = _load_json(path)
        if err or data is None:
            return Outcome("failed", detail=f"Couldn't read {path.name}; left it unchanged.")
        allow = ((data.get("permissions") or {}).get("allow")) or []
        remaining = [e for e in allow if e not in rec["details"].get("added_allow", [])]
        if remaining or len(data.get("permissions", {})) > 1 or len(data) > 1 or str(path) not in rec.get("paths_created", []):
            data.setdefault("permissions", {})["allow"] = remaining
            if not remaining:
                data["permissions"].pop("allow")
                if not data["permissions"]:
                    data.pop("permissions")
            _write_json(path, data)
        else:
            path.unlink()
        return Outcome("ok")


class CcMcp(Method):
    name = "cc-mcp"

    def _path(self, ctx) -> Path:
        return ctx.project / ".mcp.json"

    def _server(self, ctx, step) -> Dict:
        srv = ctx.catalog.mcp_servers[step["params"]["server"]]
        return {"command": srv["command"], "args": list(srv.get("args") or [])}

    def check(self, ctx, step):
        data, err = _load_json(self._path(ctx))
        if err or data is None:
            return Check(False)
        return Check(True, step["params"]["server"] in (data.get("mcpServers") or {}))

    def preview(self, ctx, step) -> Dict:
        return {"file": str(self._path(ctx)), "add_server": step["params"]["server"]}

    def install(self, ctx, step):
        path, name = self._path(ctx), step["params"]["server"]
        data, err = _load_json(path)
        if err:
            return Outcome("failed", detail=f"Didn't change .mcp.json because {err}. Nothing was overwritten.")
        b = backup(ctx, path)
        servers = data.setdefault("mcpServers", {})
        if name in servers:
            return Outcome("ok", detail="Already configured.")
        created = not path.exists()
        servers[name] = self._server(ctx, step)
        _write_json(path, data)
        return Outcome("ok", config_backups=[b] if b else [], paths_created=[str(path)] if created else [],
                       details={"file": str(path), "server": name, "config": servers[name]})

    def uninstall(self, ctx, rec):
        path, d = Path(rec["details"]["file"]), rec["details"]
        data, err = _load_json(path)
        if err or data is None:
            return Outcome("failed", detail="Couldn't read .mcp.json; left it unchanged.")
        servers = data.get("mcpServers") or {}
        if servers.get(d["server"]) == d.get("config"):
            servers.pop(d["server"])
        if not servers and str(path) in rec.get("paths_created", []) and set(data) <= {"mcpServers"}:
            path.unlink()
        else:
            _write_json(path, data)
        return Outcome("ok")


def claude_md_block(ctx: Ctx) -> str:
    plan = ctx.plan or {}
    lines = [MD_START, "## Environment (set up by env-wizard)", ""]
    for frag in plan.get("claude_md", []):
        lines.append(frag.rstrip())
        lines.append("")
    facts = plan.get("facts") or {}
    if facts:
        lines.append("Installed versions: " + ", ".join(f"{k} {v}" for k, v in sorted(facts.items()) if v))
        lines.append("")
    lines.append("To check or fix this setup, run `/env-wizard status` or `/env-wizard repair`.")
    lines.append(MD_END)
    return "\n".join(lines) + "\n"


def _strip_block(text: str) -> str:
    return re.sub(rf"\n?{re.escape(MD_START)}.*?{re.escape(MD_END)}\n?", "\n", text, flags=re.S)


class ClaudeMd(Method):
    name = "claude-md"

    def _path(self, ctx) -> Path:
        return ctx.project / "CLAUDE.md"

    def check(self, ctx, step):
        p = self._path(ctx)
        if not p.exists():
            return Check(False)
        text = p.read_text(encoding="utf-8")
        if not (ctx.plan or {}).get("steps"):
            # status/health: no plan to compare against, the block just has to be there
            return Check(MD_START in text, MD_START in text and MD_END in text)
        return Check(MD_START in text, claude_md_block(ctx) in text)

    def install(self, ctx, step):
        p = self._path(ctx)
        created = not p.exists()
        b = backup(ctx, p)
        text = "" if created else p.read_text(encoding="utf-8")
        block = claude_md_block(ctx)
        if MD_START in text:
            text = re.sub(rf"{re.escape(MD_START)}.*?{re.escape(MD_END)}\n?", lambda _: block, text, flags=re.S)
        else:
            text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        p.write_text(text, encoding="utf-8")
        return Outcome("ok", config_backups=[b] if b else [], paths_created=[str(p)] if created else [],
                       details={"file": str(p)})

    def uninstall(self, ctx, rec):
        p = Path(rec["details"]["file"])
        if not p.exists():
            return Outcome("ok")
        text = _strip_block(p.read_text(encoding="utf-8"))
        if not text.strip() and str(p) in rec.get("paths_created", []):
            p.unlink()
        else:
            p.write_text(text.strip("\n") + "\n", encoding="utf-8")
        return Outcome("ok")


class Template(Method):
    name = "template"

    def _src(self, step) -> Path:
        return plugin_root() / "templates" / step["params"]["template"]

    def _files(self, step) -> List[Path]:
        src = self._src(step)
        return sorted(p for p in src.rglob("*") if p.is_file())

    def check(self, ctx, step):
        src = self._src(step)
        present = [f for f in self._files(step) if (ctx.project / f.relative_to(src)).exists()]
        return Check(bool(present), len(present) == len(self._files(step)))

    def install(self, ctx, step):
        src = self._src(step)
        created = []
        for f in self._files(step):
            dest = ctx.project / f.relative_to(src)
            if dest.exists():
                continue  # never overwrite the user's files
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
            created.append({"path": str(dest), "sha": file_sha(dest)})
        return Outcome("ok", details={"files": created}, detail=f"Added {len(created)} starter file(s).")

    def uninstall(self, ctx, rec):
        kept = []
        for f in rec.get("details", {}).get("files", []):
            p = Path(f["path"])
            if p.exists() and file_sha(p) == f["sha"]:
                p.unlink()
            elif p.exists():
                kept.append(p.name)
        if kept:
            return Outcome("ok", detail="Kept files you changed: " + ", ".join(kept))
        return Outcome("ok")

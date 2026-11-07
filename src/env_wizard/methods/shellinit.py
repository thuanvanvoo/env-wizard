"""Shell profile / PATH edits. Every edit is a marked block so undo can remove exactly it."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from ..util import IS_WINDOWS, home
from .base import Ctx

START = "# >>> env-wizard:{name} >>>"
END = "# <<< env-wizard:{name} <<<"

SNIPPETS = {
    "local-bin": {
        "sh": ['export PATH="$HOME/.local/bin:$PATH"'],
        "ps": [],  # Windows uses the user PATH variable instead (see win_user_path)
    },
    "fnm": {
        "zsh": ['eval "$(fnm env --use-on-cd --shell zsh)"'],
        "bash": ['eval "$(fnm env --use-on-cd --shell bash)"'],
        "ps": ["fnm env --use-on-cd --shell powershell | Out-String | Invoke-Expression"],
    },
    "fnm-path": {
        "sh": ['export PATH="$HOME/.local/share/fnm:$PATH"'],
        "ps": [],
    },
}


def unix_profiles(plat: str) -> List[Path]:
    h = home()
    files = []
    if plat == "macos":
        files.append(h / ".zshrc")
        if (h / ".bash_profile").exists():
            files.append(h / ".bash_profile")
    else:
        files.append(h / ".bashrc")
        if (h / ".zshrc").exists():
            files.append(h / ".zshrc")
    return files


def _lines_for(name: str, path: Path) -> List[str]:
    snip = SNIPPETS[name]
    if "sh" in snip:
        return snip["sh"]
    return snip["zsh"] if path.name.startswith(".zsh") else snip["bash"]


def add_block(path: Path, name: str, lines: List[str]) -> bool:
    start, end = START.format(name=name), END.format(name=name)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if start in text:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n".join([start, *lines, end]) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def remove_block(path: Path, name: str) -> bool:
    if not path.exists():
        return False
    start, end = re.escape(START.format(name=name)), re.escape(END.format(name=name))
    text = path.read_text(encoding="utf-8")
    new = re.sub(rf"\n?{start}\n.*?{end}\n?", "\n", text, flags=re.S)
    if new == text:
        return False
    path.write_text(new.lstrip("\n") if not text.startswith("\n") else new, encoding="utf-8")
    return True


def powershell_profile(ctx: Ctx) -> Path:
    res = ctx.runner.run(["powershell", "-NoProfile", "-Command", "$PROFILE.CurrentUserAllHosts"])
    p = res.out.strip()
    return Path(p) if res.ok and p else home() / "Documents" / "WindowsPowerShell" / "profile.ps1"


def win_user_path(ctx: Ctx, dir_: str, add: bool) -> bool:
    script = (
        "$p=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $p){$p=''}; "
        "$parts=$p.Split(';') | Where-Object { $_ -ne '' }; $d=$env:EW_DIR; "
        + ("if($parts -notcontains $d){ $parts=@($d)+$parts; "
           "[Environment]::SetEnvironmentVariable('Path',($parts -join ';'),'User'); 'changed' }"
           if add else
           "if($parts -contains $d){ $parts=$parts | Where-Object { $_ -ne $d }; "
           "[Environment]::SetEnvironmentVariable('Path',($parts -join ';'),'User'); 'changed' }")
    )
    res = ctx.runner.run(["powershell", "-NoProfile", "-Command", script], env={"EW_DIR": dir_})
    return res.ok and "changed" in res.out


def apply_snippet(ctx: Ctx, name: str, win_dir: str = "") -> List[Dict]:
    """Add the named snippet to the user's shell startup. Returns edit records for the state file."""
    edits: List[Dict] = []
    if IS_WINDOWS or ctx.plat == "windows":
        if win_dir and win_user_path(ctx, win_dir, add=True):
            edits.append({"kind": "win-user-path", "dir": win_dir, "name": name})
        if SNIPPETS[name].get("ps"):
            prof = powershell_profile(ctx)
            if add_block(prof, name, SNIPPETS[name]["ps"]):
                edits.append({"kind": "block", "file": str(prof), "name": name})
        return edits
    for f in unix_profiles(ctx.plat):
        if add_block(f, name, _lines_for(name, f)):
            edits.append({"kind": "block", "file": str(f), "name": name})
    return edits


def undo_edit(ctx: Ctx, edit: Dict) -> bool:
    if edit.get("kind") == "block":
        return remove_block(Path(edit["file"]), edit["name"])
    if edit.get("kind") == "win-user-path":
        return win_user_path(ctx, edit["dir"], add=False)
    return False

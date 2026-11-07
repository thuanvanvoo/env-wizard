"""Environment detection: OS, arch, admin capability, proxy, disk, network."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .util import IS_WINDOWS, home, parse_version, which

# Minimum supported OS versions (SPEC §10.1). Linux/WSL are v1.1 "preview".
MIN_VERSIONS = {"macos": (13,), "windows": (10, 0, 19045), "ubuntu": (22, 4), "debian": (12,), "fedora": (39,)}
V1_PLATFORMS = {"macos", "windows"}

NETWORK_PROBES = [("github.com", 443), ("pypi.org", 443)]


def _run(argv, timeout=5) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _os_release() -> Dict[str, str]:
    out = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"')
    except OSError:
        pass
    return out


def detect_platform() -> Dict:
    system = platform.system()
    info: Dict = {"arch": platform.machine().lower()}
    if system == "Darwin":
        info["os"] = "macos"
        info["os_version"] = platform.mac_ver()[0]
        info["rosetta"] = _run(["sysctl", "-n", "sysctl.proc_translated"]) == "1"
        if info["rosetta"]:
            info["arch"] = "arm64"
    elif system == "Windows":
        info["os"] = "windows"
        info["os_version"] = platform.version()
    elif system == "Linux":
        rel = _os_release()
        is_wsl = "microsoft" in platform.release().lower() or bool(os.environ.get("WSL_DISTRO_NAME"))
        info["os"] = "wsl" if is_wsl else "linux"
        info["distro"] = rel.get("ID", "unknown")
        info["os_version"] = rel.get("VERSION_ID", "")
    else:
        info["os"] = system.lower() or "unknown"
        info["os_version"] = platform.release()
    info["supported"], info["support_note"] = _support(info)
    return info


def _support(info) -> (bool, Optional[str]):
    os_ = info["os"]
    v = parse_version(info.get("os_version", ""))
    if os_ == "macos":
        if v and v < MIN_VERSIONS["macos"]:
            return False, f"This Mac runs macOS {info['os_version']}. env-wizard needs macOS 13 (Ventura) or newer."
        return True, None
    if os_ == "windows":
        if v and v < MIN_VERSIONS["windows"]:
            return False, "env-wizard needs Windows 10 (version 22H2) or Windows 11."
        return True, None
    if os_ in ("linux", "wsl"):
        distro = info.get("distro")
        if distro in MIN_VERSIONS:
            if v and v < MIN_VERSIONS[distro]:
                return False, f"env-wizard needs {distro.title()} {'.'.join(map(str, MIN_VERSIONS[distro]))} or newer."
            return True, "Linux/WSL support is a preview (full support in v1.1)."
        return False, f"This Linux distribution ({distro}) isn't supported yet. Supported: Ubuntu 22.04+, Debian 12+, Fedora 39+."
    return False, f"This operating system ({os_}) isn't supported."


def platform_key(info: Dict) -> str:
    return info["os"]


def admin_capability(info: Dict) -> Dict:
    """Can this user get admin rights at all? (We never elevate ourselves.)"""
    os_ = info["os"]
    if os_ == "macos":
        groups = _run(["id", "-Gn"]).split()
        return {"can_admin": "admin" in groups, "elevated": os.geteuid() == 0}
    if os_ == "windows":
        whoami = _run(["whoami", "/groups"], timeout=10)
        in_admins = "S-1-5-32-544" in whoami
        elevated = "S-1-16-12288" in whoami  # High Mandatory Level
        return {"can_admin": in_admins, "elevated": elevated}
    groups = _run(["id", "-Gn"]).split()
    return {"can_admin": bool({"sudo", "wheel", "admin"} & set(groups)), "elevated": os.geteuid() == 0}


def proxy_settings() -> Dict:
    keys = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"]
    proxies = {k: v for k, v in os.environ.items() if k in keys}
    ca_keys = ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE"]
    cas = {k: v for k, v in os.environ.items() if k in ca_keys}
    return {"proxy": bool(proxies), "proxy_vars": sorted(proxies), "custom_ca": bool(cas), "ca_vars": sorted(cas)}


def tool_env(proxy: Dict) -> Dict[str, str]:
    """Environment tweaks so tools trust the system certificate store behind corporate proxies."""
    env: Dict[str, str] = {}
    if proxy.get("proxy") or proxy.get("custom_ca"):
        env["UV_NATIVE_TLS"] = "1"
        env["NODE_OPTIONS"] = (os.environ.get("NODE_OPTIONS", "") + " --use-system-ca").strip()
    return env


def disk_free_mb(path: Path) -> int:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(str(p)).free // (1024 * 1024)


def network_status(timeout: float = 3.0) -> Dict:
    results = {}
    for host, port in NETWORK_PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                results[host] = True
        except OSError:
            results[host] = False
    online = any(results.values())
    return {"online": online, "reachable": results}


def package_managers(info: Dict) -> Dict:
    return {
        "brew": which("brew"),
        "winget": which("winget"),
        "uv": which("uv"),
        "fnm": which("fnm"),
        "apt": which("apt-get") if info["os"] in ("linux", "wsl") else None,
        "dnf": which("dnf") if info["os"] in ("linux", "wsl") else None,
    }


def detect_all(project: Path, network: bool = True) -> Dict:
    info = detect_platform()
    return {
        "platform": info,
        "admin": admin_capability(info),
        "proxy": proxy_settings(),
        "disk_free_mb": {"home": disk_free_mb(home()), "project": disk_free_mb(project)},
        "network": network_status() if network else {"online": None, "reachable": {}},
        "managers": package_managers(info),
        "claude_code_in": info["os"],
        "is_windows": IS_WINDOWS,
    }

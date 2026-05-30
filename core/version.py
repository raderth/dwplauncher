"""
core/version.py  –  Local version tracking and server version checking.

The server is expected to expose:
  GET /version  →  {
    "version": "1.0.0",
    "mc_version": "1.21.1",
    "fabric_version": "0.15.0",
    "mods": {"sodium": "0.5.0", "lithium": "0.11.2", ...}
  }

Local version stored at <game_dir>/launcher_version.json:
  {
    "version": "1.0.0",
    "mc_version": "1.21.1",
    "fabric_version": "0.15.0",
    "mods": {"sodium": "0.5.0", ...}
  }
"""
import json
import re
import urllib.request
from pathlib import Path

_VERSION_FILE = "launcher_version.json"


def _parse_mc_version(version: str) -> str:
    """Extract the Minecraft version prefix from a launcher version string."""
    if not isinstance(version, str):
        return ""
    match = re.match(r"^(\d+(?:\.\d+)*)$", version)
    return match.group(1) if match else ""


def _looks_like_launcher_version(value: str) -> bool:
    """Detect a launcher version string versus a plain Minecraft version folder name."""
    if not isinstance(value, str):
        return False
    return "-" in value or any(c.isalpha() for c in value)


def _looks_like_mc_version(value: str) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"\d+(?:\.\d+)*", value))


def _infer_mc_version_from_dir(path: Path) -> str | None:
    if not isinstance(path, Path):
        return None
    if _looks_like_mc_version(path.name):
        return path.name
    if path.parent and _looks_like_mc_version(path.parent.name):
        return path.parent.name
    return None


def local_version(game_dir: str) -> dict | None:
    root = Path(game_dir)
    path = root / _VERSION_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data and isinstance(data, dict):
                if data.get("version") and not data.get("mc_version"):
                    mc_version = _infer_mc_version_from_dir(root)
                    if mc_version:
                        data["mc_version"] = mc_version
                elif data.get("version") and data.get("mc_version"):
                    if _looks_like_mc_version(root.name) and data["mc_version"] != root.name:
                        # If the current install folder is a valid Minecraft version,
                        # prefer it for file paths and Fabric lookup.
                        data["mc_version"] = root.name
            return data
        except Exception:
            pass

    root = Path(game_dir)
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda d: d.name, reverse=True):
            if child.is_dir():
                nested = child / _VERSION_FILE
                if nested.exists():
                    try:
                        data = json.loads(nested.read_text())
                        if data and isinstance(data, dict):
                            if data.get("version") and not data.get("mc_version"):
                                mc_version = _infer_mc_version_from_dir(child)
                                if mc_version:
                                    data["mc_version"] = mc_version
                                elif data.get("version") and data.get("mc_version"):
                                    if _looks_like_mc_version(child.name) and data["mc_version"] != child.name:
                                        data["mc_version"] = child.name
                        return data
                    except Exception:
                        pass

        if root.name and root.name[0].isdigit():
            if _looks_like_mc_version(root.name):
                return {"mc_version": root.name}
            return {"version": root.name}

        for child in sorted(root.iterdir(), key=lambda d: d.name, reverse=True):
            if child.is_dir() and child.name and child.name[0].isdigit():
                if _looks_like_mc_version(child.name):
                    return {"mc_version": child.name}
                return {"version": child.name}
    return None


def server_version(server_url: str, timeout: int = 8) -> dict | None:
    try:
        url = server_url.rstrip("/") + "/version"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
            return data
    except Exception:
        return None


def write_version(game_dir: str, version_data: dict):
    path = Path(game_dir) / _VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(version_data, indent=2))


def needs_update(game_dir: str, server_url: str) -> tuple[bool, str, str]:
    """
    Returns (update_needed, local_ver, server_ver).
    If server is unreachable, returns (False, local, "unknown").
    """
    local = local_version(game_dir)
    local_str = local.get("version", "none") if local else "none"

    remote = server_version(server_url)
    if remote is None:
        return False, local_str, "unknown"

    remote_str = remote.get("version", "unknown")
    return local_str != remote_str, local_str, remote_str


def mc_version_changed(game_dir: str, server_url: str) -> bool:
    """True if the Minecraft version number has changed (wipe required)."""
    local = local_version(game_dir)
    remote = server_version(server_url)
    if not local or not remote:
        return False
    return local.get("mc_version", "") != remote.get("mc_version", "")


def fabric_version_changed(game_dir: str, server_url: str) -> bool:
    """True if Fabric version changed (requires full re-download)."""
    local = local_version(game_dir)
    remote = server_version(server_url)
    if not local or not remote:
        return False
    return local.get("fabric_version", "") != remote.get("fabric_version", "")


def mods_need_update(game_dir: str, server_url: str) -> tuple[bool, dict, dict]:
    """
    Check if any mods need updating (version changed or new mods available).
    Returns (needs_update, local_mods, server_mods).
    """
    local = local_version(game_dir)
    remote = server_version(server_url)
    
    local_mods = local.get("mods", {}) if local else {}
    remote_mods = remote.get("mods", {}) if remote else {}
    
    # Check if any mod versions changed or new mods available
    for mod_name, remote_version in remote_mods.items():
        local_version_str = local_mods.get(mod_name, "")
        if local_version_str != remote_version:
            return True, local_mods, remote_mods
    
    # Check if local has mods server doesn't (user added mods locally)
    for mod_name in local_mods:
        if mod_name not in remote_mods:
            # Local mod not on server - this is ok, user can keep it
            continue
    
    return False, local_mods, remote_mods


def is_mod_only_update(game_dir: str, server_url: str) -> bool:
    """
    Returns True if only mods need updating (MC and Fabric versions match).
    This means we can do a fast mod-only sync instead of full re-download.
    """
    local = local_version(game_dir)
    remote = server_version(server_url)
    
    if not local or not remote:
        return False
    
    # MC and Fabric versions must match
    mc_match = local.get("mc_version", "") == remote.get("mc_version", "")
    fabric_match = local.get("fabric_version", "") == remote.get("fabric_version", "")
    
    if not (mc_match and fabric_match):
        return False
    
    # Check if mods actually need updating
    needs_update, _, _ = mods_need_update(game_dir, server_url)
    return needs_update
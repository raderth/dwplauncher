"""
core/config.py  –  Persistent launcher configuration (JSON).
"""
import json
import os
import sys
import platform
from pathlib import Path

DEFAULT_CONFIG = {
    "server_url":    "http://private.playdwp.net",
    "download_url": "http://private.playdwp.net:5000",
    "map_url":       "http://playdwp.net:8080",
    "game_dir":      "./my_game_files",
    "jvm_memory_mb": None,   # None = auto (50% RAM)
    "accounts":      [],     # [{uuid, username, access_token}]
    "active_account": None,
}

_CONFIG_PATH = Path.home() / ".dwp_launcher" / "config.json"


def load() -> dict:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH) as f:
                data = json.load(f)
            # Merge any new keys from DEFAULT_CONFIG
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save(cfg: dict):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_total_ram_mb() -> int:
    """Best-effort total RAM in MB."""
    try:
        import psutil
        return psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        pass
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 4096  # fallback: assume 4 GB


def default_jvm_mb() -> int:
    return max(512, get_total_ram_mb() // 2)


def resolve_game_dir(path: str) -> str:
    """Normalize a configured game directory to the active Minecraft version folder."""
    root = Path(path)
    if root.is_file():
        root = root.parent

    version_file = root / "launcher_version.json"
    if version_file.exists():
        return str(root)

    if root.is_dir():
        candidates = []
        for child in root.iterdir():
            if child.is_dir() and (child / "launcher_version.json").exists():
                candidates.append(child)

        if not candidates:
            for child in root.iterdir():
                if child.is_dir() and (child / "mods").exists() and (child / "config").exists():
                    candidates.append(child)

        if candidates:
            candidates.sort(key=lambda d: d.name, reverse=True)
            return str(candidates[0])

    return str(root)
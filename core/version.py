"""
core/version.py  –  Version tracking using remote config.
"""
import json
from pathlib import Path
from core.remote_config import fetch_remote_config

_VERSION_FILE = "launcher_version.json"

def local_version(game_dir: str) -> dict | None:
    path = Path(game_dir) / _VERSION_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None

def remote_version(remote_url: str) -> dict | None:
    try:
        return fetch_remote_config(remote_url)
    except Exception:
        return None

def needs_update(game_dir: str, remote_url: str) -> tuple[bool, str, str]:
    local = local_version(game_dir)
    local_str = local.get("version", "none") if local else "none"
    remote = remote_version(remote_url)
    if remote is None:
        return False, local_str, "unknown"
    remote_str = remote.get("version", "unknown")
    return local_str != remote_str, local_str, remote_str

def write_version(game_dir: str, version_data: dict):
    path = Path(game_dir) / _VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(version_data, indent=2))
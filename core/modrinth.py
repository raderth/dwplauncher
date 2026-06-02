"""
core/modrinth.py  –  Download mods from Modrinth (with optional CurseForge fallback).
"""
import json
import re
from pathlib import Path
from urllib.request import urlopen, Request
from core.mojang import _download_file

MODRINTH_API = "https://api.modrinth.com/v2"

def _slug_from_modrinth_url(url: str) -> str:
    """Extract 'sodium' from 'https://modrinth.com/mod/sodium' or return as slug."""
    match = re.search(r'modrinth\.com/mod/([^/?#]+)', url)
    return match.group(1) if match else url

def _get_project_versions(slug: str, mc_version: str) -> list:
    url = f"{MODRINTH_API}/project/{slug}/version?loaders=[\"fabric\"]&game_versions=[\"{mc_version}\"]"
    req = Request(url, headers={'User-Agent': 'DWP-Launcher/1.0'})
    try:
        print(f"[MODRINTH] Fetching versions from: {url}")
        with urlopen(req) as resp:
            data = json.loads(resp.read())
        print(f"[MODRINTH] Got {len(data)} versions for {slug}")
        return data
    except Exception as e:
        print(f"[MODRINTH] Error fetching {url}: {type(e).__name__}: {str(e)}")
        if hasattr(e, 'code'):
            raise ValueError(f"Modrinth HTTP {e.code} - {e.reason} for mod '{slug}' on MC {mc_version}")
        else:
            raise ValueError(f"Modrinth API error for mod '{slug}': {str(e)}")

def download_mod_from_modrinth(slug: str, mc_version: str, mods_dir: Path) -> str:
    """Download the latest version for given MC version. Returns the filename."""
    try:
        print(f"[MODRINTH] Downloading mod: {slug} for MC {mc_version}")
        versions = _get_project_versions(slug, mc_version)
    except Exception as e:
        raise ValueError(f"Failed to fetch versions for mod '{slug}': {str(e)}")
    
    if not versions:
        raise ValueError(f"No Fabric version found for mod '{slug}' on Minecraft {mc_version}")
    
    # pick the latest (first from API)
    ver = versions[0]
    file_info = ver['files'][0]  # primary
    filename = file_info['filename']
    dest = mods_dir / filename
    
    if dest.exists():
        print(f"[MODRINTH] Mod {slug} already exists at {dest}")
        return filename
    
    try:
        download_url = file_info['url']
        print(f"[MODRINTH] Downloading {filename} from {download_url}")
        _download_file(download_url, dest, file_info['hashes'].get('sha1'))
        print(f"[MODRINTH] Successfully downloaded {slug}")
    except Exception as e:
        raise ValueError(f"Failed to download mod '{slug}' ({filename}): {str(e)}")
    
    return filename

def download_mod_from_curseforge(curseforge_url: str, mc_version: str, mods_dir: Path) -> str:
    # CurseForge requires an API key and is more complex; we provide a placeholder.
    # For a full implementation you'd need to use the CurseForge API.
    # In practice you can keep the Modrinth version as primary and CurseForge as manual fallback.
    raise NotImplementedError("CurseForge direct download not implemented; use Modrinth links.")
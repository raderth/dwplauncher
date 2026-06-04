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
    """
    Temporary generic downloader.
    """

    mods_dir.mkdir(parents=True, exist_ok=True)

    # --- Treat slug as direct URL if it looks like one ---
    if slug.startswith("http://") or slug.startswith("https://"):
        url = slug
        filename = url.split("?")[0].split("/")[-1] or "downloaded_file"
        dest = mods_dir / filename

        if dest.exists():
            return filename

        print(f"[GENERIC] Downloading from {url}")

        import requests
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        return filename

def download_mod_from_curseforge(curseforge_url: str, mc_version: str, mods_dir: Path) -> str:
    # CurseForge requires an API key and is more complex; we provide a placeholder.
    # For a full implementation you'd need to use the CurseForge API.
    # In practice you can keep the Modrinth version as primary and CurseForge as manual fallback.
    raise NotImplementedError("CurseForge direct download not implemented; use Modrinth links.")
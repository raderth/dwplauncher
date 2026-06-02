"""
core/mojang.py  –  Download Minecraft client jar, libraries and assets from Mojang.
"""
import json
import os
import shutil
import hashlib
from pathlib import Path
from urllib.request import urlopen, Request
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
RESOURCES_BASE = "https://resources.download.minecraft.net/"

# Download configuration
DEFAULT_PARALLEL_WORKERS = 8
MOD_PARALLEL_WORKERS = 4

def _download_file(url: str, dest: Path, sha1: str = None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        print(f"[DOWNLOAD] Fetching: {url}")
        with urlopen(req) as resp, open(dest, 'wb') as f:
            f.write(resp.read())
        print(f"[DOWNLOAD] Saved to: {dest}")
    except Exception as e:
        print(f"[DOWNLOAD] Error downloading {url}: {type(e).__name__}: {str(e)}")
        if hasattr(e, 'code'):
            raise ValueError(f"HTTP {e.code} - {e.reason} when downloading from: {url}")
        else:
            raise ValueError(f"Failed to download from {url}: {str(e)}")
    if sha1:
        with open(dest, 'rb') as f:
            actual = hashlib.sha1(f.read()).hexdigest()
        if actual != sha1:
            os.remove(dest)
            raise ValueError(f"SHA1 mismatch for {dest.name}: expected {sha1}, got {actual}")

def get_version_manifest() -> dict:
    with urlopen(VERSION_MANIFEST_URL) as resp:
        return json.loads(resp.read())

def get_version_info(mc_version: str) -> dict:
    manifest = get_version_manifest()
    for ver in manifest['versions']:
        if ver['id'] == mc_version:
            with urlopen(ver['url']) as resp:
                return json.loads(resp.read())
    raise ValueError(f"Minecraft version {mc_version} not found")

def download_minecraft(version_info: dict, game_dir: Path):
    """Download client jar, libraries, and asset index into game_dir."""
    # Client jar
    jar_info = version_info['downloads']['client']
    jar_path = game_dir / 'versions' / version_info['id'] / f"{version_info['id']}.jar"
    _download_file(jar_info['url'], jar_path, jar_info['sha1'])

    # Libraries
    lib_dir = game_dir / 'libraries'
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for lib in version_info['libraries']:
            artifact = lib.get('downloads', {}).get('artifact')
            if not artifact:
                continue
            dest = lib_dir / artifact['path']
            if dest.exists():
                continue
            futures.append(pool.submit(_download_file, artifact['url'], dest, artifact.get('sha1')))
        for future in as_completed(futures):
            future.result()  # raise if any failed

    # Asset index
    asset_index = version_info.get('assetIndex', {})
    if asset_index:
        index_url = asset_index['url']
        index_path = game_dir / 'assets' / 'indexes' / f"{asset_index['id']}.json"
        _download_file(index_url, index_path, asset_index.get('sha1'))
        with open(index_path) as f:
            assets = json.load(f)['objects']
        assets_dir = game_dir / 'assets' / 'objects'
        with ThreadPoolExecutor(max_workers=16) as pool:
            fut2 = []
            for name, obj in assets.items():
                h = obj['hash']
                sub = h[:2]
                asset_file = assets_dir / sub / h
                if asset_file.exists():
                    continue
                fut2.append(pool.submit(_download_file, f"{RESOURCES_BASE}{sub}/{h}", asset_file))
            for future in as_completed(fut2):
                future.result()

def install_minecraft(game_dir: Path, mc_version: str):
    print(f"Downloading Minecraft {mc_version}...")
    version_info = get_version_info(mc_version)
    
    # ── NEW: Save the version JSON so Fabric inheritance works ──
    version_json_path = game_dir / "versions" / mc_version / f"{mc_version}.json"
    version_json_path.parent.mkdir(parents=True, exist_ok=True)
    version_json_path.write_text(json.dumps(version_info, indent=2))
    
    download_minecraft(version_info, game_dir)

def download_mod_direct(url: str, dest_dir: Path) -> str:
    """Download a mod directly from a URL and return the filename.
    
    This handles direct mod links (not via Modrinth API).
    Filename is extracted from URL or generated.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    # Extract filename from URL
    filename = url.split('/')[-1].split('?')[0]
    if not filename or '.' not in filename:
        filename = f"mod_{hash(url) % 10000}.jar"
    
    dest = dest_dir / filename
    if dest.exists():
        print(f"[DIRECT] Mod already exists: {filename}")
        return filename
    
    print(f"[DIRECT] Downloading direct mod link: {url}")
    _download_file(url, dest)
    print(f"[DIRECT] Downloaded: {filename}")
    return filename
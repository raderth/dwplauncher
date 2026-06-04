"""
core/installer.py  –  Full installation/update process using official sources.
"""
import json
import threading
import shutil
import subprocess
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from core import mojang, modrinth, remote_config
from core.version import write_version
from core.mojang import download_mod_direct
from core.game_launcher import find_java

class Installer:
    def __init__(self, game_dir: str, remote_config_url: str,
                 on_progress=None, on_status=None, on_done=None, on_error=None):
        self.game_dir = Path(game_dir)
        self.remote_url = remote_config_url
        self.on_progress = on_progress or (lambda *a: None)
        self.on_status = on_status or (lambda m: None)
        self.on_done = on_done or (lambda: None)
        self.on_error = on_error or (lambda m: None)
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # Patch mojang._download_file to echo status into the UI while we run.
        import core.mojang as _mojang
        _orig_dl = _mojang._download_file
        _status_cb = self.on_status

        def _status_dl(url, dest, sha1=None):
            filename = Path(url).name
            _status_cb(f"Downloading {filename}…")
            return _orig_dl(url, dest, sha1)

        _mojang._download_file = _status_dl

        try:
            self._run_inner()
        finally:
            _mojang._download_file = _orig_dl

    def _cleanup_stale_fabric_profiles(self):
        """Remove fabric profile JSONs with fewer than 5 libraries (broken installs)."""
        versions_root = self.game_dir / "versions"
        if not versions_root.exists():
            return
        for d in versions_root.iterdir():
            if not (d.is_dir() and d.name.startswith("fabric-loader-")):
                continue
            json_path = d / f"{d.name}.json"
            if not json_path.exists():
                continue
            try:
                data = json.loads(json_path.read_text())
                if len(data.get("libraries", [])) < 5:
                    print(f"[INSTALLER] Removing stale fabric profile: {d.name}")
                    import shutil as _shutil
                    _shutil.rmtree(d)
            except Exception:
                pass

    def _run_inner(self):
        try:
            self.on_status("Fetching remote config...")
            print(f"[INSTALLER] Remote config URL: {self.remote_url}")
            self._cleanup_stale_fabric_profiles()
            config = remote_config.fetch_remote_config(self.remote_url)
            remote_version = config['version']
            mc_version = config['mc_version']
            mods = config.get('mods', [])
            
            print(f"[INSTALLER] Remote version: {remote_version}, MC version: {mc_version}")
            print(f"[INSTALLER] Mods to install: {len(mods)}")

            local_ver = self._read_local_version()
            if local_ver == remote_version and self._all_mods_present(mods, mc_version):
                self.on_status("Already up-to-date.")
                self.on_done()
                return

            # 1. Minecraft
            if not self._is_minecraft_installed(mc_version) or local_ver != remote_version:
                self.on_status(f"Downloading Minecraft {mc_version}...")
                print(f"[INSTALLER] Installing Minecraft {mc_version}...")
                mojang.install_minecraft(self.game_dir, mc_version)

            # 2. Fabric loader (from config URL)
            fabric_loader_url = config.get('fabric_loader_url')
            if not fabric_loader_url:
                raise ValueError("Missing 'fabric_loader_url' in remote config")
            print(f"[INSTALLER] Fabric loader URL: {fabric_loader_url}")
            loader_filename = Path(fabric_loader_url).name
            loader_dest = self.game_dir / 'versions' / mc_version / loader_filename
            if not loader_dest.exists() or local_ver != remote_version:
                self.on_status(f"Downloading Fabric loader: {loader_filename}")
                try:
                    from core.mojang import _download_file
                    _download_file(fabric_loader_url, loader_dest)
                except Exception as e:
                    self.on_error(f"Failed to download Fabric loader from {fabric_loader_url}: {str(e)}")
                    raise
            
            # Set up Fabric version profile using the official Fabric meta API.
            # We fetch the real launcher profile JSON from:
            #   https://meta.fabricmc.net/v2/versions/loader/<mc>/<loader>/profile/json
            # This gives us the complete library list (ASM, mixin, tiny-remapper,
            # access-widener, etc.) that Fabric needs at runtime — something a
            # hand-crafted minimal JSON can never provide correctly.
            self.on_status("Setting up Fabric...")

            # Extract loader version from filename, stripping any trailing dot.
            match = re.search(r'fabric-loader-([\d.]+)', loader_filename)
            loader_version = match.group(1).rstrip('.') if match else None
            if not loader_version:
                raise ValueError(f"Cannot determine Fabric loader version from filename: {loader_filename}")

            fabric_profile_name = f"fabric-loader-{loader_version}-{mc_version}"
            versions_root       = self.game_dir / 'versions'
            fabric_profile_dir  = versions_root / fabric_profile_name
            fabric_profile_dir.mkdir(parents=True, exist_ok=True)
            version_json = fabric_profile_dir / f"{fabric_profile_name}.json"

            # Always (re)fetch the profile JSON when it is missing or was written
            # by a previous broken run (detectable by having < 5 libraries).
            def _profile_needs_fetch(path: Path) -> bool:
                if not path.exists():
                    return True
                try:
                    data = json.loads(path.read_text())
                    return len(data.get("libraries", [])) < 5
                except Exception:
                    return True

            if _profile_needs_fetch(version_json):
                meta_url = (
                    f"https://meta.fabricmc.net/v2/versions/loader"
                    f"/{mc_version}/{loader_version}/profile/json"
                )
                self.on_status(f"Fetching Fabric profile for {loader_version}…")
                print(f"[INSTALLER] Fetching Fabric profile JSON: {meta_url}")
                try:
                    from urllib.request import urlopen, Request
                    req = Request(meta_url, headers={"User-Agent": "DWP-Launcher/1.0"})
                    with urlopen(req, timeout=30) as resp:
                        profile_data = json.loads(resp.read())
                    version_json.write_text(json.dumps(profile_data, indent=2))
                    lib_count = len(profile_data.get("libraries", []))
                    print(f"[INSTALLER] Fabric profile saved ({lib_count} libraries): {version_json}")
                except Exception as e:
                    raise ValueError(
                        f"Failed to fetch Fabric profile from {meta_url}: {e}\n"
                        f"Check that loader version {loader_version} exists for MC {mc_version}."
                    )
            else:
                lib_count = len(json.loads(version_json.read_text()).get("libraries", []))
                print(f"[INSTALLER] Fabric profile already present ({lib_count} libraries): {version_json}")

            # The profile JSON's libraries use bare {"name": ..., "url": ...} entries
            # (no downloads.artifact block) — that is normal and game_launcher.py's
            # _build_classpath now handles it correctly by reading the "url" field
            # when building the fallback download URL.

            # 3. Mods
            self.on_status("Updating mods...")
            mods_dir = self.game_dir / 'mods'
            mods_dir.mkdir(exist_ok=True)
            
            mod_tasks = []
            for i, mod_entry in enumerate(mods):
                if self._stop.is_set():
                    break
                
                if isinstance(mod_entry, str):
                    mod_tasks.append(('string', mod_entry, i))
                elif isinstance(mod_entry, dict):
                    direct_url = mod_entry.get('url')
                    modrinth_entry = mod_entry.get('modrinth')
                    if direct_url:
                        print(f"[INSTALLER] Direct download: {direct_url}")
                        mod_tasks.append(('direct', direct_url, i))
                    elif modrinth_entry:
                        print(f"[INSTALLER] Direct download (via modrinth field): {modrinth_entry}")
                        mod_tasks.append(('direct', modrinth_entry, i))
            
            total_mods = len(mod_tasks)
            completed = 0
            
            def download_mod_task(task_type, task_data, index):
                nonlocal completed
                try:
                    if task_type == 'direct':
                        filename = download_mod_direct(task_data, mods_dir)
                        return (True, filename, None)
                    elif task_type == 'string':
                        if 'modrinth.com' in task_data:
                            slug = modrinth._slug_from_modrinth_url(task_data)
                            modrinth.download_mod_from_modrinth(slug, mc_version, mods_dir)
                            return (True, slug, None)
                        else:
                            filename = download_mod_direct(task_data, mods_dir)
                            return (True, filename, None)
                    elif task_type == 'modrinth':
                        modrinth.download_mod_from_modrinth(task_data, mc_version, mods_dir)
                        return (True, task_data, None)
                except Exception as e:
                    return (False, None, str(e))
            
            if mod_tasks:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = {}
                    for task_type, task_data, idx in mod_tasks:
                        future = pool.submit(download_mod_task, task_type, task_data, idx)
                        futures[future] = (task_type, task_data)
                    
                    for future in as_completed(futures):
                        completed += 1
                        success, result, error = future.result()
                        self.on_progress(completed, total_mods, completed, total_mods)
                        
                        if not success:
                            self.on_error(f"Failed to download mod: {error}")
                        else:
                            print(f"[INSTALLER] Downloaded mod: {result}")
            
            self.on_progress(len(mods), len(mods), len(mods), len(mods))

            # 4. Write version file
            write_version(str(self.game_dir), {
                'version': remote_version,
                'mc_version': mc_version,
                'mods': {modrinth._slug_from_modrinth_url(m['modrinth']): 'unknown' for m in mods if isinstance(m, dict) and m.get('modrinth')}
            })
            self.on_status("Installation complete.")
            self.on_done()
        except Exception as e:
            print(f"[INSTALLER] Error: {type(e).__name__}: {str(e)}")
            self.on_error(str(e))

    def _read_local_version(self):
        try:
            with open(self.game_dir / 'launcher_version.json') as f:
                return json.load(f).get('version', None)
        except Exception:
            return None

    def _is_minecraft_installed(self, mc_version):
        return (self.game_dir / 'versions' / mc_version / f'{mc_version}.jar').exists()

    def _is_fabric_installed(self, mc_version):
        return any((self.game_dir / 'versions' / mc_version).glob('fabric-loader-*.jar'))

    def _all_mods_present(self, mods, mc_version):
        return False
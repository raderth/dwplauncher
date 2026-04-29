"""
core/downloader.py  –  Download, verify and repair game files.

FULLY PRESERVED (never touched):
  saves/**, options.txt, servers.dat, screenshots/**, logs/**, crash-reports/**

SYNC-ONLY (add missing, never delete or overwrite):
  mods/**, resourcepacks/**, datapacks/**, shaderpacks/**
  - A server mod is considered "present" if the .jar OR the .jar.disabled
    version exists locally. This preserves the user's enabled/disabled toggle.
  - User-added files that are NOT in the server manifest are left alone.
  - Files that are in the manifest but missing entirely are downloaded.

NORMAL (hash-verified, re-downloaded if wrong):
  Everything else (game JARs, configs, libraries, assets, etc.)
"""
import hashlib
import json
import os
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from core import version
import zipfile
import io
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor

_BATCH_SIZE    = 300   # safety cap (rarely hit given the size limit)
_BATCH_MAX_MB  = 200    # ~10 batches total for a full 2GB install

_FULLY_PRESERVE = [
    "saves",
    "options.txt",
    "servers.dat",
    "screenshots",
    "logs",
    "crash-reports",
]

_SYNC_ONLY_DIRS = [
    "mods",
    "resourcepacks",
    "datapacks",
    "shaderpacks",
]


def _is_fully_preserved(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return any(parts[0] == g or rel_path == g for g in _FULLY_PRESERVE)


def _is_sync_only(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return len(parts) > 0 and parts[0] in _SYNC_ONLY_DIRS


def _sync_present(dest: Path) -> bool:
    """
    True if the file (or its .disabled / enabled counterpart) exists locally.
    We don't check the hash — user may have a different version or toggled it.
    """
    if dest.exists():
        return True
    # enabled → check disabled variant
    disabled = dest.parent / (dest.name + ".disabled")
    if disabled.exists():
        return True
    # disabled → check enabled variant
    if dest.name.endswith(".disabled"):
        enabled = dest.parent / dest.name[: -len(".disabled")]
        if enabled.exists():
            return True
    return False


class Downloader:
    def __init__(
        self,
        server_url: str,
        game_dir:   str,
        on_progress: Optional[Callable] = None,
        on_status:   Optional[Callable] = None,
        on_phase:    Optional[Callable] = None,
        on_done:     Optional[Callable] = None,
        on_error:    Optional[Callable] = None,
        repair_only: bool = False,
        max_concurrent_batches=4,
    ):
        self.server_url  = server_url.rstrip("/")
        self.game_dir    = Path(game_dir)
        self.on_progress = on_progress or (lambda *a: None)
        self.on_status   = on_status   or (lambda m: None)
        self.on_phase    = on_phase    or (lambda p: None)
        self.on_done     = on_done     or (lambda: None)
        self.on_error    = on_error    or (lambda m: None)
        self.repair_only = repair_only
        self._stop       = threading.Event()

        self.max_concurrent = max_concurrent_batches
        self._loop = None

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            manifest = self._fetch_manifest()

            if self.repair_only:
                to_download = self._find_bad(manifest)
            else:
                to_download = self._find_missing_or_bad(manifest)

            self._download_files(to_download, total_manifest=manifest)

            # Verify only normal (non-sync-only, non-preserved) files
            verify_set = [
                e for e in manifest
                if not _is_fully_preserved(e["path"]) and not _is_sync_only(e["path"])
            ]
            failed = self._verify(verify_set)
            if failed:
                self.on_status(f"Repairing {len(failed)} bad file(s)…")
                self._download_files(failed, total_manifest=manifest)
                still_bad = self._verify(verify_set)
                if still_bad:
                    raise RuntimeError(
                        "Verification failed for: "
                        + ", ".join(e["path"] for e in still_bad[:5])
                    )

            self._write_version()
            self.on_phase("done")
            self.on_done()
        except Exception as exc:
            self.on_phase("error")
            self.on_error(str(exc))

    def _fetch_manifest(self) -> list[dict]:
        self.on_status("Fetching manifest…")
        with urllib.request.urlopen(f"{self.server_url}/manifest", timeout=15) as r:
            data = json.loads(r.read())
        self.on_status(f"Manifest: {len(data)} files")
        return data

    def _find_missing_or_bad(self, manifest: list[dict]) -> list[dict]:
        to_dl = []
        for entry in manifest:
            rel  = entry["path"]
            dest = self.game_dir / rel

            if _is_fully_preserved(rel):
                continue

            if _is_sync_only(rel):
                # Only download if neither the enabled nor disabled copy exists
                if not _sync_present(dest):
                    to_dl.append(entry)
                continue

            # Normal file — download if missing or hash wrong
            if not dest.exists() or self._hash_file(dest) != entry["hash"]:
                to_dl.append(entry)

        return to_dl

    def _find_bad(self, manifest: list[dict]) -> list[dict]:
        """Repair mode: same rules but also re-checks hash on normal files."""
        bad = []
        for entry in manifest:
            rel  = entry["path"]
            dest = self.game_dir / rel

            if _is_fully_preserved(rel):
                continue

            if _is_sync_only(rel):
                if not _sync_present(dest):
                    bad.append(entry)
                continue

            if not dest.exists() or self._hash_file(dest) != entry["hash"]:
                bad.append(entry)

        return bad

    def _download_files(self, entries, total_manifest):
        if not entries:
            self.on_status("Nothing to download.")
            return
        self.on_phase("downloading")

        # Build hash → entries (handles duplicates, as already fixed)
        hash_to_entries = {}
        for e in entries:
            hash_to_entries.setdefault(e["hash"], []).append(e)

        total_bytes   = sum(e.get("size", 0) for e in entries)
        bytes_done    = 0
        files_done    = 0
        total_files   = len(entries)

        # Split into batches as before
        batches = []
        current = []
        current_mb = 0.0
        for e in entries:
            current.append(e)
            current_mb += e.get("size", 0) / 1_048_576
            if len(current) >= _BATCH_SIZE or current_mb >= _BATCH_MAX_MB:
                batches.append(current)
                current = []
                current_mb = 0.0
        if current:
            batches.append(current)

        # We'll run async code inside a new event loop in this thread
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(
                self._async_download_batches(
                    batches, hash_to_entries,
                    total_bytes, total_files
                )
            )
        finally:
            loop.close()

        self.on_status("Download complete.")

    async def _async_download_batches(self, batches, hash_to_entries,
                                    total_bytes, total_files):
        semaphore = asyncio.Semaphore(self.max_concurrent)
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0))
        lock = asyncio.Lock()
        progress = {"bytes": 0, "files": 0}

        async def download_one_batch(batch_idx, batch):
            async with semaphore:
                if self._stop.is_set():
                    raise RuntimeError("Cancelled")

                hashes = list({e["hash"] for e in batch})
                batch_n = f"{batch_idx+1}/{len(batches)}"
                self.on_status(f"Downloading batch {batch_n} ({len(batch)} files)…")

                async with session.post(
                    f"{self.server_url}/batch",
                    json={"hashes": hashes},
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    raw = await resp.read()

                # Run blocking extraction in a thread to keep the loop free
                loop = asyncio.get_running_loop()
                def process_zip():
                    processed_bytes = 0
                    processed_files = 0
                    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                        for name in zf.namelist():
                            entries_for_hash = hash_to_entries.get(name)
                            if not entries_for_hash:
                                continue
                            data = zf.read(name)
                            for entry in entries_for_hash:
                                dest = self.game_dir / entry["path"]
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(data)
                                processed_bytes += entry.get("size", 0)
                                processed_files += 1
                    return processed_bytes, processed_files

                batch_bytes, batch_files = await loop.run_in_executor(None, process_zip)

                # Update shared counters under lock
                async with lock:
                    progress["bytes"] += batch_bytes
                    progress["files"] += batch_files
                self.on_progress(progress["files"], total_files,
                                progress["bytes"], total_bytes)

        tasks = [download_one_batch(i, batch) for i, batch in enumerate(batches)]
        try:
            await asyncio.gather(*tasks)
        except RuntimeError:
            pass
        finally:
            await session.close()


    def _verify(self, manifest: list[dict]) -> list[dict]:
        self.on_phase("verifying")
        bad   = []
        total = len(manifest)
        for idx, entry in enumerate(manifest):
            if self._stop.is_set():
                break
            rel  = entry["path"]
            dest = self.game_dir / rel
            self.on_status(f"Verifying [{idx+1}/{total}] {rel}")
            if not dest.exists() or self._hash_file(dest) != entry["hash"]:
                bad.append(entry)
            self.on_progress(idx + 1, total, idx + 1, total)
        return bad

    def _write_version(self):
        try:
            remote = version.server_version(self.server_url)
            if remote and isinstance(remote, dict) and remote.get("version"):
                data = dict(remote)
                if not data.get("mc_version"):
                    mc = version._infer_mc_version_from_dir(self.game_dir)
                    if mc:
                        data["mc_version"] = mc
                version.write_version(str(self.game_dir), data)
        except Exception:
            pass

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
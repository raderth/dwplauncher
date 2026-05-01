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
import asyncio
import hashlib
import io
import json
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import aiohttp

from core import version

# Maximum simultaneous chunk downloads.  4 is a safe default; bump to 6-8 on
# fast LAN connections where the server isn't the bottleneck.
_MAX_CONCURRENT = 4

# How many files to hash in parallel during verify.
# I/O-bound, so more threads than CPU cores is fine.
_VERIFY_WORKERS = 8

_FULLY_PRESERVE = [
    "saves", "options.txt", "servers.dat",
    "screenshots", "logs", "crash-reports",
]
_SYNC_ONLY_DIRS = ["mods", "resourcepacks", "datapacks", "shaderpacks"]


# ── Path classification ───────────────────────────────────────────────────────

def _is_fully_preserved(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return any(parts[0] == g or rel_path == g for g in _FULLY_PRESERVE)


def _is_sync_only(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    return len(parts) > 0 and parts[0] in _SYNC_ONLY_DIRS


def _sync_present(dest: Path) -> bool:
    """True if the file or its .disabled counterpart exists locally."""
    if dest.exists():
        return True
    disabled = dest.parent / (dest.name + ".disabled")
    if disabled.exists():
        return True
    if dest.name.endswith(".disabled"):
        enabled = dest.parent / dest.name[: -len(".disabled")]
        if enabled.exists():
            return True
    return False


# ── Downloader ────────────────────────────────────────────────────────────────

class Downloader:
    def __init__(
        self,
        server_url:           str,
        game_dir:             str,
        on_progress:          Optional[Callable] = None,
        on_status:            Optional[Callable] = None,
        on_phase:             Optional[Callable] = None,
        on_done:              Optional[Callable] = None,
        on_error:             Optional[Callable] = None,
        repair_only:          bool = False,
        max_concurrent_chunks: int = _MAX_CONCURRENT,
    ):
        self.server_url   = server_url.rstrip("/")
        self.game_dir     = Path(game_dir)
        self.on_progress  = on_progress or (lambda *a: None)
        self.on_status    = on_status   or (lambda m: None)
        self.on_phase     = on_phase    or (lambda p: None)
        self.on_done      = on_done     or (lambda: None)
        self.on_error     = on_error    or (lambda m: None)
        self.repair_only  = repair_only
        self.max_concurrent = max_concurrent_chunks
        self._stop        = threading.Event()

    def stop(self):
        self._stop.set()

    # ── Public entry-point ────────────────────────────────────────────────

    def run(self):
        try:
            manifest, server_chunks = self._fetch_manifest()

            if self.repair_only:
                needed_hashes = self._find_bad_hashes(manifest)
            else:
                needed_hashes = self._find_missing_or_bad_hashes(manifest)

            if needed_hashes:
                # Resolve which server chunks contain our needed files so we
                # only fetch the chunks we actually need.
                chunks_to_fetch = _filter_chunks(server_chunks, needed_hashes)
                self._download_chunks(chunks_to_fetch, needed_hashes, manifest)
            else:
                self.on_status("Nothing to download.")

            # Post-download integrity check (normal files only).
            verify_set = [
                e for e in manifest
                if not _is_fully_preserved(e["path"])
                and not _is_sync_only(e["path"])
            ]
            failed = self._verify(verify_set)
            if failed:
                self.on_status(f"Repairing {len(failed)} bad file(s)…")
                self._repair_individual(failed)
                still_bad = self._verify(failed)   # re-check only the repaired set
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

    # ── Manifest ──────────────────────────────────────────────────────────

    def _fetch_manifest(self) -> tuple[list[dict], list[dict]]:
        self.on_status("Fetching manifest…")
        with urllib.request.urlopen(f"{self.server_url}/manifest", timeout=15) as r:
            data = json.loads(r.read())
        # Support both the new {files, chunks} format and the old flat list.
        if isinstance(data, list):
            files  = data
            chunks = []
        else:
            files  = data.get("files", [])
            chunks = data.get("chunks", [])
        self.on_status(f"Manifest: {len(files)} files in {len(chunks)} chunks")
        return files, chunks

    # ── Diff computation ──────────────────────────────────────────────────

    def _find_missing_or_bad_hashes(self, manifest: list[dict]) -> set[str]:
        needed: set[str] = set()
        for entry in manifest:
            rel  = entry["path"]
            dest = self.game_dir / rel
            if _is_fully_preserved(rel):
                continue
            if _is_sync_only(rel):
                if not _sync_present(dest):
                    needed.add(entry["hash"])
                continue
            if not dest.exists() or self._hash_file(dest) != entry["hash"]:
                needed.add(entry["hash"])
        return needed

    def _find_bad_hashes(self, manifest: list[dict]) -> set[str]:
        """Repair mode — same logic, explicit name for clarity."""
        return self._find_missing_or_bad_hashes(manifest)

    # ── Chunk download ────────────────────────────────────────────────────

    def _download_chunks(
        self,
        chunks:        list[dict],
        needed_hashes: set[str],
        manifest:      list[dict],
    ) -> None:
        if not chunks:
            # Server is old-format (no chunk map). Fall back to legacy batch.
            self._legacy_batch_download(
                [e for e in manifest if e["hash"] in needed_hashes],
                manifest,
            )
            return

        self.on_phase("downloading")

        # Build hash → [entry] for fast lookup when extracting.
        hash_to_entries: dict[str, list[dict]] = {}
        for e in manifest:
            hash_to_entries.setdefault(e["hash"], []).append(e)

        total_files = sum(
            len([h for h in c["hashes"] if h in needed_hashes])
            for c in chunks
        )
        total_bytes = sum(
            e["size"]
            for e in manifest
            if e["hash"] in needed_hashes
        )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                self._async_fetch_chunks(
                    chunks, needed_hashes, hash_to_entries,
                    total_files, total_bytes,
                )
            )
        finally:
            loop.close()

        self.on_status("Download complete.")

    async def _async_fetch_chunks(
        self,
        chunks:          list[dict],
        needed_hashes:   set[str],
        hash_to_entries: dict[str, list[dict]],
        total_files:     int,
        total_bytes:     int,
    ) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        executor  = ThreadPoolExecutor(max_workers=self.max_concurrent)
        lock      = asyncio.Lock()
        progress  = {"files": 0, "bytes": 0}

        # Use a single connector with a generous pool.
        connector = aiohttp.TCPConnector(
            limit=self.max_concurrent * 2,
            limit_per_host=self.max_concurrent * 2,
        )
        async with aiohttp.ClientSession(connector=connector) as session:

            async def fetch_one(chunk_idx: int, chunk: dict):
                async with semaphore:
                    if self._stop.is_set():
                        return

                    chunk_id  = chunk["id"]
                    chunk_hashes = [h for h in chunk["hashes"] if h in needed_hashes]
                    if not chunk_hashes:
                        return   # nothing we need in this chunk

                    label = f"{chunk_idx + 1}/{len(chunks)}"
                    self.on_status(
                        f"Downloading chunk {label} "
                        f"({len(chunk_hashes)} files)…"
                    )

                    # ── Stream the zip, writing to a BytesIO as it arrives ──
                    # This overlaps network I/O and disk I/O: we start
                    # extracting as soon as the download finishes, while
                    # the next chunk is already downloading in parallel.
                    buf = io.BytesIO()
                    async with session.get(
                        f"{self.server_url}/chunk/{chunk_id}",
                        timeout=aiohttp.ClientTimeout(total=300),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(
                                f"Chunk {chunk_id} returned HTTP {resp.status}"
                            )
                        # Read in large chunks to keep TCP windows full.
                        async for raw in resp.content.iter_chunked(1 << 17):  # 128 KB
                            buf.write(raw)

                    # ── Extract in a thread to keep the event loop free ────
                    loop = asyncio.get_running_loop()

                    def extract():
                        written_bytes = 0
                        written_files = 0
                        buf.seek(0)
                        with zipfile.ZipFile(buf) as zf:
                            for name in zf.namelist():
                                if name not in chunk_hashes:
                                    continue   # file not needed this run
                                entries = hash_to_entries.get(name, [])
                                if not entries:
                                    continue
                                data = zf.read(name)
                                for entry in entries:
                                    dest = self.game_dir / entry["path"]
                                    dest.parent.mkdir(parents=True, exist_ok=True)
                                    dest.write_bytes(data)
                                    written_bytes += entry.get("size", 0)
                                    written_files += 1
                        return written_bytes, written_files

                    b, f = await loop.run_in_executor(executor, extract)

                    async with lock:
                        progress["bytes"] += b
                        progress["files"] += f

                    self.on_progress(
                        progress["files"], total_files,
                        progress["bytes"], total_bytes,
                    )

            tasks = [
                asyncio.create_task(fetch_one(i, c))
                for i, c in enumerate(chunks)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        executor.shutdown(wait=False)

        # Surface any exceptions (cancelled counts as RuntimeError above).
        for r in results:
            if isinstance(r, Exception) and not self._stop.is_set():
                raise r

    # ── Legacy batch fallback (old server format) ─────────────────────────

    def _legacy_batch_download(
        self,
        entries:        list[dict],
        total_manifest: list[dict],
    ) -> None:
        """Keeps compatibility with servers that don't expose /chunk."""
        self.on_phase("downloading")

        hash_to_entries: dict[str, list[dict]] = {}
        for e in entries:
            hash_to_entries.setdefault(e["hash"], []).append(e)

        total_bytes = sum(e.get("size", 0) for e in entries)
        total_files = len(entries)

        batches: list[list[dict]] = []
        cur: list[dict] = []
        cur_mb = 0.0
        for e in entries:
            cur.append(e)
            cur_mb += e.get("size", 0) / 1_048_576
            if len(cur) >= 300 or cur_mb >= 200:
                batches.append(cur)
                cur, cur_mb = [], 0.0
        if cur:
            batches.append(cur)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                self._async_legacy_batches(
                    batches, hash_to_entries, total_files, total_bytes
                )
            )
        finally:
            loop.close()

        self.on_status("Download complete.")

    async def _async_legacy_batches(
        self,
        batches:         list[list[dict]],
        hash_to_entries: dict[str, list[dict]],
        total_files:     int,
        total_bytes:     int,
    ) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        lock      = asyncio.Lock()
        progress  = {"files": 0, "bytes": 0}

        async with aiohttp.ClientSession() as session:

            async def do_batch(idx: int, batch: list[dict]):
                async with semaphore:
                    if self._stop.is_set():
                        return
                    hashes = list({e["hash"] for e in batch})
                    self.on_status(
                        f"Downloading batch {idx + 1}/{len(batches)} "
                        f"({len(batch)} files)…"
                    )
                    async with session.post(
                        f"{self.server_url}/batch",
                        json={"hashes": hashes},
                        timeout=aiohttp.ClientTimeout(total=300),
                    ) as resp:
                        raw = await resp.read()

                    loop = asyncio.get_running_loop()

                    def process_zip():
                        b = f = 0
                        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                            for name in zf.namelist():
                                for entry in hash_to_entries.get(name, []):
                                    dest = self.game_dir / entry["path"]
                                    dest.parent.mkdir(parents=True, exist_ok=True)
                                    dest.write_bytes(zf.read(name))
                                    b += entry.get("size", 0)
                                    f += 1
                        return b, f

                    pb, pf = await loop.run_in_executor(None, process_zip)
                    async with lock:
                        progress["bytes"] += pb
                        progress["files"] += pf
                    self.on_progress(
                        progress["files"], total_files,
                        progress["bytes"], total_bytes,
                    )

            await asyncio.gather(*[
                asyncio.create_task(do_batch(i, b))
                for i, b in enumerate(batches)
            ])

    # ── Individual file repair (used after verify failure) ────────────────

    def _repair_individual(self, entries: list[dict]) -> None:
        """
        Re-downloads individual files via /file/<hash>.
        Used when a small number of files fail the post-download verify.
        Shows per-file progress so the UI isn't blank.
        """
        self.on_phase("downloading")
        total = len(entries)
        for idx, entry in enumerate(entries):
            if self._stop.is_set():
                break
            rel  = entry["path"]
            dest = self.game_dir / rel
            self.on_status(f"Repairing [{idx + 1}/{total}] {rel}")
            try:
                with urllib.request.urlopen(
                    f"{self.server_url}/file/{entry['hash']}", timeout=60
                ) as r:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.read())
            except Exception as e:
                raise RuntimeError(f"Failed to repair {rel}: {e}") from e
            self.on_progress(idx + 1, total, idx + 1, total)

    # ── Verify (parallel) ─────────────────────────────────────────────────

    def _verify(self, manifest: list[dict]) -> list[dict]:
        """
        Hash-check all files in *manifest* using a thread pool.
        Returns entries that are missing or have wrong hashes.
        Progress is reported after each file so the UI stays responsive.
        """
        self.on_phase("verifying")
        total = len(manifest)
        if total == 0:
            return []

        bad:      list[dict]  = []
        lock      = threading.Lock()
        done_count            = [0]   # mutable int via list

        def check_one(entry: dict) -> dict | None:
            if self._stop.is_set():
                return None
            rel  = entry["path"]
            dest = self.game_dir / rel
            result = None
            if not dest.exists() or self._hash_file(dest) != entry["hash"]:
                result = entry
            with lock:
                done_count[0] += 1
                n = done_count[0]
            # Status update only every 10 files to avoid flooding the UI thread.
            if n % 10 == 0 or n == total:
                self.on_status(f"Verifying {n}/{total} files…")
            self.on_progress(n, total, n, total)
            return result

        with ThreadPoolExecutor(max_workers=_VERIFY_WORKERS) as pool:
            for result in pool.map(check_one, manifest):
                if result is not None:
                    bad.append(result)

        return bad

    # ── Version writing ───────────────────────────────────────────────────

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

    # ── File hashing ──────────────────────────────────────────────────────

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _filter_chunks(
    server_chunks: list[dict],
    needed_hashes: set[str],
) -> list[dict]:
    """Return only chunks that contain at least one needed hash."""
    return [c for c in server_chunks if any(h in needed_hashes for h in c["hashes"])]
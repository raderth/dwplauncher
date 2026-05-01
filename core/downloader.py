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
import logging
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import aiohttp

from core import version

log = logging.getLogger(__name__)

_MAX_CONCURRENT = 4
_VERIFY_WORKERS = 8

# If more than this fraction of a chunk's files are needed, fetch the whole
# chunk rather than repairing individual files.  0.3 = 30 %.
_CHUNK_REPAIR_THRESHOLD = 0.30

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
    if dest.exists():
        return True
    if (dest.parent / (dest.name + ".disabled")).exists():
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
        server_url:            str,
        game_dir:              str,
        on_progress:           Optional[Callable] = None,
        on_status:             Optional[Callable] = None,
        on_phase:              Optional[Callable] = None,
        on_done:               Optional[Callable] = None,
        on_error:              Optional[Callable] = None,
        repair_only:           bool = False,
        max_concurrent_chunks: int  = _MAX_CONCURRENT,
    ):
        self.server_url    = server_url.rstrip("/")
        self.game_dir      = Path(game_dir)
        self.on_progress   = on_progress or (lambda *a: None)
        self.on_status     = on_status   or (lambda m: None)
        self.on_phase      = on_phase    or (lambda p: None)
        self.on_done       = on_done     or (lambda: None)
        self.on_error      = on_error    or (lambda m: None)
        self.repair_only   = repair_only
        self.max_concurrent = max_concurrent_chunks
        self._stop         = threading.Event()

    def stop(self):
        self._stop.set()

    # ── Entry-point ───────────────────────────────────────────────────────

    def run(self):
        try:
            log.info("Downloader.run() started  game_dir=%s", self.game_dir)
            manifest, server_chunks = self._fetch_manifest()

            needed_hashes = self._find_missing_or_bad_hashes(manifest)
            log.info("Need %d hashes  chunks_available=%d",
                     len(needed_hashes), len(server_chunks))

            if needed_hashes:
                chunks_to_fetch = _filter_chunks(server_chunks, needed_hashes)
                log.info("Fetching %d/%d chunks", len(chunks_to_fetch), len(server_chunks))
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
                log.info("%d files failed verify — running smart repair", len(failed))
                self.on_status(f"Repairing {len(failed)} file(s)…")
                self._smart_repair(failed, server_chunks, manifest)
                still_bad = self._verify(failed)
                if still_bad:
                    raise RuntimeError(
                        "Verification failed for: "
                        + ", ".join(e["path"] for e in still_bad[:5])
                    )

            self._write_version()
            self.on_phase("done")
            self.on_done()

        except Exception as exc:
            log.exception("Downloader.run() failed")
            self.on_phase("error")
            self.on_error(str(exc))

    # ── Manifest ──────────────────────────────────────────────────────────

    def _fetch_manifest(self) -> tuple[list[dict], list[dict]]:
        self.on_status("Fetching manifest…")
        url = f"{self.server_url}/manifest"
        log.info("GET %s", url)
        with urllib.request.urlopen(url, timeout=15) as r:
            raw = r.read()
        log.info("Manifest response: %d bytes", len(raw))
        data = json.loads(raw)
        if isinstance(data, list):
            files, chunks = data, []
        else:
            files  = data.get("files", [])
            chunks = data.get("chunks", [])
        log.info("Manifest parsed: %d files, %d chunks", len(files), len(chunks))
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

    # ── Chunk download ────────────────────────────────────────────────────

    def _download_chunks(
        self,
        chunks:        list[dict],
        needed_hashes: set[str],
        manifest:      list[dict],
    ) -> None:
        if not chunks:
            log.warning("No chunks available — falling back to legacy /batch")
            self._legacy_batch_download(
                [e for e in manifest if e["hash"] in needed_hashes],
                manifest,
            )
            return

        self.on_phase("downloading")

        hash_to_entries: dict[str, list[dict]] = {}
        for e in manifest:
            hash_to_entries.setdefault(e["hash"], []).append(e)

        total_files = sum(
            sum(1 for h in c["hashes"] if h in needed_hashes)
            for c in chunks
        )
        total_bytes = sum(
            e["size"] for e in manifest if e["hash"] in needed_hashes
        )
        log.info("Download plan: %d files, %.1f MB across %d chunks",
                 total_files, total_bytes / 1_048_576, len(chunks))

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

        connector = aiohttp.TCPConnector(
            limit=self.max_concurrent * 2,
            limit_per_host=self.max_concurrent * 2,
        )

        async def fetch_one(chunk_idx: int, chunk: dict):
            async with semaphore:
                if self._stop.is_set():
                    return

                chunk_id = chunk["id"]
                n_needed = sum(1 for h in chunk["hashes"] if h in needed_hashes)
                if n_needed == 0:
                    log.debug("Chunk %s: nothing needed, skipping", chunk_id)
                    return

                label = f"{chunk_idx + 1}/{len(chunks)}"
                self.on_status(f"Downloading chunk {label} ({n_needed} files)…")
                url = f"{self.server_url}/chunk/{chunk_id}"
                log.info("GET %s  (%d files needed)", url, n_needed)

                buf = io.BytesIO()
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=300),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(
                                f"Chunk {chunk_id} returned HTTP {resp.status}"
                            )
                        content_length = resp.headers.get("Content-Length", "unknown")
                        log.info("Chunk %s: status=%d content-length=%s",
                                 chunk_id, resp.status, content_length)
                        async for raw in resp.content.iter_chunked(1 << 17):
                            buf.write(raw)

                received = buf.tell()
                log.info("Chunk %s: received %d bytes", chunk_id, received)

                if received == 0:
                    raise RuntimeError(f"Chunk {chunk_id}: server sent 0 bytes")

                # Capture loop variables explicitly so the closure is unambiguous.
                _buf       = buf
                _needed    = needed_hashes
                _h2e       = hash_to_entries
                _game_dir  = self.game_dir
                _chunk_id  = chunk_id
                _received  = received

                loop = asyncio.get_running_loop()

                def extract() -> tuple[int, int]:
                    written_bytes = 0
                    written_files = 0
                    _buf.seek(0)
                    try:
                        zf = zipfile.ZipFile(_buf)
                    except zipfile.BadZipFile as exc:
                        raise RuntimeError(
                            f"Chunk {_chunk_id} is not a valid zip "
                            f"(received {_received} bytes): {exc}"
                        ) from exc

                    names = zf.namelist()
                    log.info("Chunk %s zip: %d entries", _chunk_id, len(names))

                    with zf:
                        for name in names:
                            if name not in _needed:
                                continue
                            entries = _h2e.get(name, [])
                            if not entries:
                                log.warning("Chunk %s: hash %s in zip but "
                                            "not in manifest", _chunk_id, name[:16])
                                continue
                            try:
                                data = zf.read(name)
                            except Exception as exc:
                                log.error("Chunk %s: read error for %s: %s",
                                          _chunk_id, name[:16], exc)
                                raise
                            for entry in entries:
                                dest = _game_dir / entry["path"]
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(data)
                                written_bytes += entry.get("size", 0)
                                written_files += 1

                    log.info("Chunk %s: wrote %d/%d files",
                             _chunk_id, written_files, len(names))
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

        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            executor.shutdown(wait=True)
            await connector.close()

        for r in results:
            if isinstance(r, Exception) and not self._stop.is_set():
                raise r

    # ── Smart repair (chunk-aware) ────────────────────────────────────────

    def _smart_repair(
        self,
        failed:        list[dict],
        server_chunks: list[dict],
        manifest:      list[dict],
    ) -> None:
        """
        For each server chunk, if >= CHUNK_REPAIR_THRESHOLD of its files need
        repair, fetch the whole chunk (fast).  The remainder go file-by-file.
        """
        if not server_chunks:
            self._repair_individual(failed)
            return

        failed_hashes = {e["hash"] for e in failed}
        covered_by_chunk: set[str] = set()

        do_chunks:     list[dict] = []
        do_individual: list[dict] = []

        for chunk in server_chunks:
            chunk_total  = len(chunk["hashes"])
            chunk_needed = sum(1 for h in chunk["hashes"] if h in failed_hashes)
            if chunk_needed == 0:
                continue
            ratio = chunk_needed / chunk_total
            log.info("Repair chunk %s: %d/%d files bad (%.0f%%)",
                     chunk["id"], chunk_needed, chunk_total, ratio * 100)
            if ratio >= _CHUNK_REPAIR_THRESHOLD:
                do_chunks.append(chunk)
                covered_by_chunk.update(chunk["hashes"])
            # else individual — handled below

        for e in failed:
            if e["hash"] not in covered_by_chunk:
                do_individual.append(e)

        log.info("Repair plan: %d full chunks, %d individual files",
                 len(do_chunks), len(do_individual))

        if do_chunks:
            self._download_chunks(do_chunks, failed_hashes, manifest)

        if do_individual:
            self._repair_individual(do_individual)

    # ── Individual file repair ────────────────────────────────────────────

    def _repair_individual(self, entries: list[dict]) -> None:
        """Re-download single files via /file/<hash>. Progress per file."""
        self.on_phase("downloading")
        total = len(entries)
        log.info("Individual repair: %d files", total)
        for idx, entry in enumerate(entries):
            if self._stop.is_set():
                break
            rel  = entry["path"]
            dest = self.game_dir / rel
            self.on_status(f"Repairing [{idx + 1}/{total}] {rel}")
            url = f"{self.server_url}/file/{entry['hash']}"
            log.info("GET %s -> %s", url, dest)
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            except Exception as exc:
                raise RuntimeError(f"Failed to repair {rel}: {exc}") from exc
            self.on_progress(idx + 1, total, idx + 1, total)

    # ── Legacy batch fallback ─────────────────────────────────────────────

    def _legacy_batch_download(
        self,
        entries:        list[dict],
        total_manifest: list[dict],
    ) -> None:
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
                        if resp.status != 200:
                            raise RuntimeError(f"/batch returned HTTP {resp.status}")
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

    # ── Verify (parallel) ─────────────────────────────────────────────────

    def _verify(self, manifest: list[dict]) -> list[dict]:
        self.on_phase("verifying")
        total = len(manifest)
        if total == 0:
            return []

        bad:        list[dict] = []
        lock        = threading.Lock()
        done_count  = [0]

        def check_one(entry: dict):
            if self._stop.is_set():
                return None
            dest = self.game_dir / entry["path"]
            result = None
            if not dest.exists() or self._hash_file(dest) != entry["hash"]:
                result = entry
            with lock:
                done_count[0] += 1
                n = done_count[0]
            if n % 10 == 0 or n == total:
                self.on_status(f"Verifying {n}/{total} files…")
            self.on_progress(n, total, n, total)
            return result

        with ThreadPoolExecutor(max_workers=_VERIFY_WORKERS) as pool:
            for result in pool.map(check_one, manifest):
                if result is not None:
                    bad.append(result)

        log.info("Verify complete: %d/%d bad", len(bad), total)
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

def _filter_chunks(server_chunks: list[dict], needed_hashes: set[str]) -> list[dict]:
    return [c for c in server_chunks if any(h in needed_hashes for h in c["hashes"])]
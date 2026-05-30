#!/usr/bin/env python3
"""
server.py  –  Hash every file in a directory, pre-build zip chunks at startup,
              and serve them via Flask.

Usage:
    python server.py --dir ./my_content --port 5000

Endpoints:
    GET  /manifest          → JSON: { files: [{hash, path, size}], chunks: [{id, hashes}] }
    GET  /chunk/<chunk_id>  → raw zip bytes (pre-built at startup)
    GET  /file/<sha256>     → raw file bytes  (single-file fallback / repair)
    GET  /health            → { status, files, chunks }
    GET  /version           → { version, mc_version, fabric_version, mods: {mod_name: version} }
"""

import hashlib
import io
import json
import os
import sys
import zipfile
import argparse
from pathlib import Path
import tempfile
import shutil
import re

from flask import Flask, Response, jsonify, send_file, abort, stream_with_context

app = Flask(__name__)

VERSION      = "1.1.0"
BASE_DIR: Path = Path(".")

# Populated at startup — never mutated afterwards, so no locking needed.
MANIFEST:      list[dict]       = []   # [{hash, path, size}, …]
HASH_TO_PATH:  dict[str, Path]  = {}   # sha256 → absolute Path
CHUNKS:        list[dict]       = []   # [{id, hashes: [...]}]
CHUNK_DIR: Path = None
VERSION_INFO:  dict             = {}   # {mc_version, fabric_version, mods: {name: version}}

# Tuning — adjust to taste.
_CHUNK_MAX_FILES = 300
_CHUNK_MAX_MB    = 200


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compression_for(name: str) -> int:
    """Already-compressed formats gain nothing from DEFLATE."""
    NO_COMPRESS = {".jar", ".zip", ".png", ".jpg", ".ogg", ".mp3", ".gz", ".xz"}
    return (zipfile.ZIP_STORED
            if Path(name).suffix.lower() in NO_COMPRESS
            else zipfile.ZIP_DEFLATED)


def _extract_mod_version(jar_path: Path) -> str | None:
    """Extract version from fabric.mod.json inside a JAR."""
    try:
        with zipfile.ZipFile(jar_path, "r") as z:
            if "fabric.mod.json" in z.namelist():
                meta = json.loads(z.read("fabric.mod.json"))
                return meta.get("version")
    except Exception:
        pass
    return None


def _extract_versions_from_manifest(base: Path, manifest: list[dict]) -> dict:
    """
    Extract Minecraft version, Fabric version, and mod versions from manifest.
    - MC version: from versions/<version>/ or <version>/ folder
    - Fabric version: from fabric-loader or fabric-api JAR
    - Mods: from mods/ folder
    """
    result = {
        "mc_version": "",
        "fabric_version": "",
        "mods": {}
    }
    
    # Find MC version from versions/ or root version folder
    for entry in manifest:
        path = entry["path"]
        parts = Path(path).parts
        if len(parts) >= 2 and parts[0] in ("versions", ):
            if re.match(r'^\d+(\.\d+)*$', parts[1]):
                result["mc_version"] = parts[1]
                break
        elif len(parts) >= 1 and re.match(r'^\d+(\.\d+)*$', parts[0]):
            result["mc_version"] = parts[0]
            break
    
    # Find Fabric version from loader/API JAR
    fabric_path = base / "mods" / "fabric-loader.jar"
    if fabric_path.exists():
        ver = _extract_mod_version(fabric_path)
        if ver:
            result["fabric_version"] = ver
    
    fabric_api_path = base / "mods" / "fabric-api.jar"
    if fabric_api_path.exists():
        ver = _extract_mod_version(fabric_api_path)
        if ver:
            result["fabric_version"] = ver
    
    # Extract mod versions from manifest (mods/ folder)
    for entry in manifest:
        path = entry["path"]
        parts = Path(path).parts
        if len(parts) >= 2 and parts[0] == "mods" and parts[1].endswith(".jar"):
            mod_name = parts[1][:-4]  # remove .jar
            jar_path = base / path
            if jar_path.exists():
                ver = _extract_mod_version(jar_path)
                if ver:
                    result["mods"][mod_name] = ver
    
    return result


def _build_zip_to_disk(hashes: list[str], hash_to_path: dict[str, Path], dest: Path) -> None:
    """Build a zip on disk."""
    with zipfile.ZipFile(dest, "w") as zf:
        for h in hashes:
            path = hash_to_path.get(h)
            if path and path.exists():
                zf.write(path, arcname=h,
                         compress_type=_compression_for(path.name),
                         compresslevel=6)


# ── Startup indexing ──────────────────────────────────────────────────────────

def index_and_prebuild(base: Path) -> None:
    """
    1. Walk *base*, hash every file → MANIFEST / HASH_TO_PATH.
    2. Split into chunks and compress each one into memory.
    All globals are replaced atomically at the end so Flask never sees
    a half-initialised state if you ever call this again at runtime.
    """
    global CHUNK_DIR
    CHUNK_DIR = Path(tempfile.mkdtemp(prefix="mcchunks_"))
    print(f"[server] Chunk cache: {CHUNK_DIR}")
    print(f"[server] Indexing '{base}' …", flush=True)
    manifest: list[dict]      = []
    hash_to_path: dict[str, Path] = {}

    for root, _dirs, files in os.walk(base):
        for fname in sorted(files):
            abs_path = Path(root) / fname
            rel_path = abs_path.relative_to(base).as_posix()
            file_hash = _hash_file(abs_path)
            size = abs_path.stat().st_size
            manifest.append({"hash": file_hash, "path": rel_path, "size": size})
            hash_to_path[file_hash] = abs_path

    print(f"[server] Indexed {len(manifest)} files — building zip chunks …", flush=True)

    # ── Chunk splitting (same logic as downloader) ────────────────────────
    chunks: list[dict]        = []

    current_hashes: list[str] = []
    current_mb = 0.0

    def flush_chunk():
        nonlocal current_hashes, current_mb
        if not current_hashes:
            return
        chunk_id = f"chunk_{len(chunks):04d}"
        print(f"[server]   Building {chunk_id} …", flush=True)
        dest_path = CHUNK_DIR / f"{chunk_id}.zip"
        _build_zip_to_disk(current_hashes, hash_to_path, dest_path)
        chunks.append({"id": chunk_id, "hashes": list(current_hashes)})
        current_hashes = []
        current_mb = 0.0

    for entry in manifest:
        current_hashes.append(entry["hash"])
        current_mb += entry["size"] / 1_048_576
        if len(current_hashes) >= _CHUNK_MAX_FILES or current_mb >= _CHUNK_MAX_MB:
            flush_chunk()
    flush_chunk()

    # Extract version information from manifest
    version_info = _extract_versions_from_manifest(base, manifest)

    # Atomic swap — Flask threads reading the old globals are unaffected.
    global MANIFEST, HASH_TO_PATH, CHUNKS, VERSION_INFO
    MANIFEST, HASH_TO_PATH, CHUNKS, VERSION_INFO = \
        manifest, hash_to_path, chunks, version_info

    # Calculate total size from the ON-DISK zips, not from memory
    total_zip_mb = sum(
        (CHUNK_DIR / f"{c['id']}.zip").stat().st_size
        for c in chunks
    ) / 1_048_576

    print(f"[server] Ready — {len(manifest)} files in {len(chunks)} chunks "
          f"({total_zip_mb:.1f} MB pre-compressed)", flush=True)
    print(f"[server] MC: {version_info.get('mc_version', 'unknown')}, "
          f"Fabric: {version_info.get('fabric_version', 'unknown')}, "
          f"Mods: {len(version_info.get('mods', {}))} available", flush=True)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/manifest")
def manifest():
    """
    Returns the full file list AND the chunk map so clients can decide
    which chunks to fetch rather than computing batches themselves.
    """
    return jsonify({"files": MANIFEST, "chunks": CHUNKS})


@app.route("/chunk/<chunk_id>")
def serve_chunk(chunk_id: str):
    zip_path = CHUNK_DIR / f"{chunk_id}.zip"
    if not zip_path.exists():
        abort(404)
    return send_file(zip_path, mimetype="application/zip")


@app.route("/file/<sha256>")
def serve_file(sha256: str):
    """Single-file fallback — used by repair / selective re-download."""
    path = HASH_TO_PATH.get(sha256)
    if path is None or not str(path.resolve()).startswith(str(BASE_DIR)):
        abort(404, description=f"No file with hash {sha256}")
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "files":  len(MANIFEST),
        "chunks": len(CHUNKS),
    })


@app.route("/version")
def version_route():
    """
    Returns version info including MC version, Fabric version, and mod versions.
    Format: {
        "version": "1.1.0",
        "mc_version": "1.21.1",
        "fabric_version": "0.15.0",
        "mods": {
            "sodium": "0.5.0",
            "lithium": "0.11.2",
            ...
        }
    }
    """
    response = {"version": VERSION}
    response.update(VERSION_INFO)
    return jsonify(response)


# ── Entry-point ───────────────────────────────────────────────────────────────

def main():
    global BASE_DIR
    parser = argparse.ArgumentParser(description="File-hash server")
    parser.add_argument("--dir",  default="./content",
                        help="Directory to serve (default: ./content)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    BASE_DIR = Path(args.dir).resolve()
    if not BASE_DIR.is_dir():
        print(f"[server] ERROR: '{BASE_DIR}' is not a directory.")
        sys.exit(1)

    index_and_prebuild(BASE_DIR)

    # Save manifest for debugging / CDN pre-seeding.
    manifest_path = BASE_DIR.parent / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump({"files": MANIFEST, "chunks": CHUNKS}, f, indent=2)
    print(f"[server] Manifest saved → {manifest_path}")
    print(f"[server] Listening on http://{args.host}:{args.port}")

    # Use Waitress if available (cross-platform, production-grade).
    # Fall back to Werkzeug dev server with threading enabled.
    try:
        from waitress import serve as waitress_serve
        print("[server] Using waitress WSGI server")
        waitress_serve(app, host=args.host, port=args.port, threads=16)
    except ImportError:
        print("[server] waitress not installed — using Flask dev server "
              "(install waitress for production: pip install waitress)")
        app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
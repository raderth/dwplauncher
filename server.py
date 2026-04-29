#!/usr/bin/env python3
"""
server.py  –  Hash every file in a directory and serve them via Flask.

Usage:
    python server.py --dir ./my_content --port 5000

Endpoints:
    GET /manifest          → JSON list of {hash, path, size}
    GET /file/<sha256>     → raw file bytes
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from flask import Flask, jsonify, send_file, abort
import io
import zipfile
from flask import Flask, jsonify, send_file, abort, request

app = Flask(__name__)

# Server version for client to check for updates
VERSION = "1.0.0"

# Populated at startup
MANIFEST: list[dict] = []          # [{hash, rel_path, size}, ...]
HASH_TO_PATH: dict[str, Path] = {} # sha256 → absolute Path
BASE_DIR: Path = Path(".")


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex-digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def index_directory(base: Path) -> None:
    """Walk *base*, hash every file, build MANIFEST and HASH_TO_PATH."""
    global MANIFEST, HASH_TO_PATH
    MANIFEST = []
    HASH_TO_PATH = {}
    for root, _dirs, files in os.walk(base):
        for fname in sorted(files):
            abs_path = Path(root) / fname
            rel_path = abs_path.relative_to(base).as_posix()
            file_hash = hash_file(abs_path)
            size = abs_path.stat().st_size
            entry = {"hash": file_hash, "path": rel_path, "size": size}
            MANIFEST.append(entry)
            HASH_TO_PATH[file_hash] = abs_path
    print(f"[server] Indexed {len(MANIFEST)} files from '{base}'")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/manifest")
def manifest():
    """Return the full file manifest as JSON."""
    return jsonify(MANIFEST)

@app.route("/batch", methods=["POST"])
def batch():
    """
    POST body: {"hashes": ["abc123", "def456", ...]}
    Returns a zip containing files named by their hash.
    """
    body = request.get_json(force=True, silent=True) or {}
    hashes = body.get("hashes", [])
    if not hashes:
        abort(400, "No hashes requested")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for h in hashes:
            path = HASH_TO_PATH.get(h)
            if path and path.exists():
                zf.write(path, arcname=h,
                        compress_type=_compression_for(path.name),
                        compresslevel=6)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", download_name="batch.zip")

def _compression_for(name: str):
    # Already-compressed formats — storing is faster than trying to compress
    NO_COMPRESS = {".jar", ".zip", ".png", ".jpg", ".ogg", ".mp3"}
    return zipfile.ZIP_STORED if Path(name).suffix.lower() in NO_COMPRESS else zipfile.ZIP_DEFLATED

@app.route("/file/<sha256>")
def serve_file(sha256: str):
    """Stream the file that matches *sha256*."""
    path = HASH_TO_PATH.get(sha256)
    if path is None:
        abort(404, description=f"No file with hash {sha256}")
    return send_file(path, as_attachment=True,
                     download_name=path.name)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "files": len(MANIFEST)})


@app.route("/version")
def version():
    return jsonify({"version": VERSION})


# ── Entry-point ───────────────────────────────────────────────────────────────

def main():
    global BASE_DIR
    parser = argparse.ArgumentParser(description="File-hash server")
    parser.add_argument("--dir",  default="./content",
                        help="Directory to serve (default: ./content)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port to listen on (default: 5000)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind (default: 0.0.0.0)")
    args = parser.parse_args()

    BASE_DIR = Path(args.dir).resolve()
    if not BASE_DIR.is_dir():
        print(f"[server] ERROR: '{BASE_DIR}' is not a directory.")
        sys.exit(1)

    index_directory(BASE_DIR)

    manifest_path = BASE_DIR.parent / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(MANIFEST, f, indent=2)
    print(f"[server] Manifest saved → {manifest_path}")
    print(f"[server] Listening on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
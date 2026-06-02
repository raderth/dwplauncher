"""
core/game_launcher.py  –  Locate Java and launch Fabric/Minecraft.

Java lookup (in order):
  1. User‑specified custom_java (from settings)
  2. Bundled: <game_dir>/java/<os>/bin/java  (or javaw.exe on Windows)
  3. System:  JAVA_HOME or PATH (with macOS /usr/libexec/java_home support)

Version JSON / JAR lookup:
  The Fabric installer creates versions/fabric-loader-<lv>-<mc>/<id>.json
  and the vanilla JAR lives at versions/<mc>/<mc>.jar.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path

log = logging.getLogger("launcher")

MAVEN_REPOS = [
    "https://libraries.minecraft.net/",
    "https://maven.fabricmc.net/",
    "https://repo1.maven.org/maven2/",
]

MAIN_CLASS = "net.fabricmc.loader.impl.launch.knot.KnotClient"


# ── OS helpers ────────────────────────────────────────────────────────────────

def _os_name() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    return "linux"


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


# ── Java detection ────────────────────────────────────────────────────────────

def find_java(game_dir: str, custom_java: str | None = None) -> Path | None:
    """
    Returns the path to a Java executable.
    If `custom_java` is provided and points to a valid file, it is used immediately.
    Otherwise the usual auto‑detection order is followed.
    """
    # 1. User‑specified path
    if custom_java:
        candidate = Path(custom_java)
        if candidate.exists() and candidate.is_file():
            log.info("Using custom Java: %s", candidate)
            return candidate
        log.warning(
            "Custom Java path not found: %s – falling back to auto‑detection",
            custom_java,
        )

    # 2. Bundled Java
    base = Path(game_dir).resolve() / "java" / _os_name() / "bin"
    exe_names = ["javaw.exe", "java.exe"] if _is_windows() else ["java"]

    for name in exe_names:
        candidate = base / name
        if candidate.exists():
            log.info("Bundled Java: %s", candidate)
            return candidate

    # 3. JAVA_HOME environment variable
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        java_exe = Path(java_home) / "bin" / ("javaw.exe" if _is_windows() else "java")
        if java_exe.exists():
            return java_exe

    # 4. System PATH
    system_java = shutil.which("javaw.exe" if _is_windows() else "java")
    if system_java:
        return Path(system_java)

    # 5. macOS helper
    if platform.system() == "Darwin":
        try:
            home = subprocess.check_output(
                ["/usr/libexec/java_home"], text=True
            ).strip()
            exe = Path(home) / "bin" / "java"
            if exe.exists():
                return exe
        except Exception:
            pass

    log.error("No Java found")
    return None


# ── Fabric version ID resolution ──────────────────────────────────────────────

def find_fabric_version_id(game_dir: str, mc_ver: str) -> str | None:
    """
    Returns the Fabric-loader version directory name that matches mc_ver,
    e.g. 'fabric-loader-0.16.9-1.21.1'.
    Falls back to mc_ver itself if no Fabric profile exists yet.
    """
    versions_root = Path(game_dir).resolve() / "versions"
    if not versions_root.is_dir():
        return None

    candidates = [
        d
        for d in versions_root.iterdir()
        if d.is_dir()
        and d.name.startswith("fabric-loader-")
        and mc_ver in d.name
    ]
    if candidates:
        candidates.sort(key=lambda d: d.name, reverse=True)
        return candidates[0].name

    vanilla = versions_root / mc_ver
    if vanilla.is_dir():
        return mc_ver

    return None


# ── Classpath builder ─────────────────────────────────────────────────────────

def _build_classpath(
    game_dir_path: Path, version_id: str, mc_ver: str
) -> list[str] | None:
    classpath: list[str] = []
    lib_dir = game_dir_path / "libraries"

    version_json_path = (
        game_dir_path / "versions" / version_id / f"{version_id}.json"
    )
    if not version_json_path.exists():
        log.error("Missing version JSON: %s", version_json_path)
        return None

    version_data = json.loads(version_json_path.read_text(encoding="utf-8"))

    # Fabric loader JSON inherits from the vanilla profile
    inherits = version_data.get("inheritsFrom")
    if inherits:
        parent_path = game_dir_path / "versions" / inherits / f"{inherits}.json"
        if parent_path.exists():
            parent_data = json.loads(parent_path.read_text(encoding="utf-8"))
            libs = parent_data.get("libraries", []) + version_data.get(
                "libraries", []
            )
        else:
            libs = version_data.get("libraries", [])
    else:
        libs = version_data.get("libraries", [])

    log.info("Libraries to resolve: %d", len(libs))

    seen: set[str] = set()
    for lib in libs:
        lib_file = None
        download_url = None

        # Path 1: standard Mojang/vanilla format — downloads.artifact block
        artifact = lib.get("downloads", {}).get("artifact", {})
        if artifact.get("path"):
            lib_file = lib_dir / artifact["path"]
            download_url = artifact.get("url") or None

        # Path 2: Fabric profile format — bare {"name": ..., "url": ...}
        if (not lib_file or not lib_file.exists()) and "name" in lib:
            lib_file = lib_dir / _maven_to_path(lib["name"])
            url_suffix = str(_maven_to_path(lib["name"])).replace("\\", "/")
            repo_base = lib.get("url", "").rstrip("/")
            if repo_base:
                download_url = f"{repo_base}/{url_suffix}"
            else:
                download_url = MAVEN_REPOS[0] + url_suffix

        if lib_file and not lib_file.exists() and download_url:
            log.info("Downloading library: %s", download_url)
            _download_file(download_url, lib_file)

        if lib_file and lib_file.exists():
            key = str(lib_file)
            if key not in seen:
                seen.add(key)
                classpath.append(key)
        elif lib_file:
            log.warning(
                "Library not found and could not be downloaded: %s", lib_file
            )

    # Vanilla JAR
    vanilla_jar = game_dir_path / "versions" / mc_ver / f"{mc_ver}.jar"
    if vanilla_jar.exists() and str(vanilla_jar) not in seen:
        classpath.append(str(vanilla_jar))

    log.info("Classpath: %d entries", len(classpath))
    return classpath


# ── Maven / download helpers ──────────────────────────────────────────────────

def _maven_to_path(lib_name: str) -> Path:
    parts = lib_name.split(":")
    if len(parts) < 3:
        return Path("invalid") / lib_name
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    filename = f"{artifact}-{version}{classifier}.jar"
    return Path(*group.split(".")) / artifact / version / filename


def _download_file(url: str, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "DWP-Launcher/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            f.write(r.read())
        return True
    except Exception as exc:
        log.error("DOWNLOAD FAILED: %s", url)
        log.exception(exc)          # This prints the full stack trace
        return False


# ── Launch command ────────────────────────────────────────────────────────────

def build_launch_command(
    game_dir: str,
    mc_ver: str,
    jvm_mb: int = 2048,
    username: str = "Player",
    access_token: str = "0",
    uuid: str = "00000000-0000-0000-0000-000000000000",
    custom_java: str | None = None,          # <-- new
) -> list[str] | None:

    java = find_java(game_dir, custom_java)   # <-- pass custom path
    if java is None:
        log.error("Java not found")
        return None

    game_dir_path = Path(game_dir).resolve()
    version_id = find_fabric_version_id(game_dir, mc_ver)
    if version_id is None:
        log.error("No installed Fabric/MC version found for %s", mc_ver)
        return None

    log.info("Using version profile: %s", version_id)
    classpath = _build_classpath(game_dir_path, version_id, mc_ver)
    if not classpath:
        return None

    cp_str = os.pathsep.join(classpath)

    # Determine main class – Fabric uses KnotClient; pure vanilla uses its own
    version_json = (
        game_dir_path / "versions" / version_id / f"{version_id}.json"
    )
    main_class = MAIN_CLASS
    if version_json.exists():
        vdata = json.loads(version_json.read_text())
        main_class = vdata.get("mainClass", MAIN_CLASS)

    cmd = [
        str(java),
        f"-Xmx{jvm_mb}M",
        f"-Xms{max(512, jvm_mb // 4)}M",
        "-XX:+UseG1GC",
        f"-Djava.library.path={game_dir_path / 'natives'}",
        "-cp",
        cp_str,
        main_class,
        "--gameDir",
        str(game_dir_path),
        "--assetsDir",
        str(game_dir_path / "assets"),
        "--assetIndex",
        _asset_index_id(game_dir_path, version_id, mc_ver),
        "--version",
        version_id,
        "--username",
        username,
        "--uuid",
        uuid,
        "--accessToken",
        access_token,
        "--userType",
        "msa" if access_token != "0" else "legacy",
    ]

    if platform.system() == "Darwin":
        cmd.insert(1, "-XstartOnFirstThread")

    return cmd


def _asset_index_id(game_dir_path: Path, version_id: str, mc_ver: str) -> str:
    """Read assetIndex.id from the version JSON; fall back to mc_ver."""
    for vid in (version_id, mc_ver):
        vj = game_dir_path / "versions" / vid / f"{vid}.json"
        if vj.exists():
            try:
                data = json.loads(vj.read_text())
                aid = data.get("assetIndex", {}).get("id")
                if aid:
                    return aid
            except Exception:
                pass
    return mc_ver

def find_all_java_installations(game_dir: str) -> list[Path]:
    """
    Return a list of all Java executables we can discover on the system,
    without duplicates.  The order is:
      - Bundled Java (game_dir/java/<os>/bin/)
      - JAVA_HOME
      - System PATH (via shutil.which)
      - macOS helper (/usr/libexec/java_home -V)
    """
    candidates: list[Path] = []

    # 1. Bundled
    base = Path(game_dir).resolve() / "java" / _os_name() / "bin"
    exe_names = ["javaw.exe", "java.exe"] if _is_windows() else ["java"]
    for name in exe_names:
        p = base / name
        if p.is_file() and p not in candidates:
            candidates.append(p)

    # 2. JAVA_HOME
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        exe = Path(java_home) / "bin" / ("javaw.exe" if _is_windows() else "java")
        if exe.is_file() and exe not in candidates:
            candidates.append(exe)

    # 3. System PATH – sometimes there are multiple (but shutil.which only returns one)
    system_java = shutil.which("javaw.exe" if _is_windows() else "java")
    if system_java:
        p = Path(system_java)
        if p.is_file() and p not in candidates:
            candidates.append(p)

    # 4. macOS – /usr/libexec/java_home -V lists all installed JVMs
    if platform.system() == "Darwin":
        try:
            output = subprocess.check_output(
                ["/usr/libexec/java_home", "-V"], stderr=subprocess.STDOUT, text=True
            )
            # Lines look like: "    17.0.1 (x86_64) \"Oracle Corporation\" ...
            import re
            for line in output.splitlines():
                # Extract path inside quotes after the version info
                match = re.search(r'"([^"]+)"', line)
                if match:
                    jvm_path = Path(match.group(1)) / "bin" / "java"
                    if jvm_path.is_file() and jvm_path not in candidates:
                        candidates.append(jvm_path)
        except Exception:
            pass

    # 5. Common locations on Linux (quick scan, no recursion)
    if platform.system() == "Linux":
        for root_dir in (Path("/usr/lib/jvm"), Path("/usr/java")):
            if root_dir.is_dir():
                for jdk_dir in root_dir.iterdir():
                    exe = jdk_dir / "bin" / "java"
                    if exe.is_file() and exe not in candidates:
                        candidates.append(exe)

    # 6. Windows – common install dirs
    if _is_windows():
        for base_dir in (
            Path("C:/Program Files/Java"),
            Path("C:/Program Files (x86)/Java"),
        ):
            if base_dir.is_dir():
                for jdk_dir in base_dir.iterdir():
                    for exe_name in ("javaw.exe", "java.exe"):
                        exe = jdk_dir / "bin" / exe_name
                        if exe.is_file() and exe not in candidates:
                            candidates.append(exe)

    return candidates

# ── Public launch function ────────────────────────────────────────────────────

def launch(
    game_dir: str,
    mc_ver: str,
    jvm_mb: int = 2048,
    username: str = "Player",
    access_token: str = "0",
    uuid: str = "00000000-0000-0000-0000-000000000000",
    custom_java: str | None = None,          # <-- new
) -> tuple[str | None, subprocess.Popen | None]:
    """
    Launch the game in a detached process.
    Returns (None, process) on success, or (error_string, None) on failure.
    """
    cmd = build_launch_command(
        game_dir, mc_ver, jvm_mb, username, access_token, uuid, custom_java
    )
    if cmd is None:
        if find_java(game_dir, custom_java) is None:
            return (
                "Java not found. Please install Java 21+ or place bundled Java "
                "in game_dir/java/<os>/bin/",
                None,
            )
        return (
            f"Could not build launch command for {mc_ver} — check logs.",
            None,
        )

    kwargs: dict = {}
    if _is_windows():
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    cwd = str(Path(game_dir).resolve())
    log_path = Path(game_dir) / "launcher_debug.log"

    with open(log_path, "w") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log_file,
            stderr=log_file,
            **kwargs,
        )

    log.info("Game launched (PID=%d). Output → %s", process.pid, log_path)
    return None, process
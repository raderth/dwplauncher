"""
core/game_launcher.py  –  Locate bundled Java and launch Fabric/Minecraft.

Java layout:
  <game_dir>/java/windows/bin/java.exe  (Windows – no console window)
  <game_dir>/java/linux/bin/java
  <game_dir>/java/macos/bin/java

Fabric JAR location (checked in order):
  <game_dir>/versions/<mc_version>/<name>.jar   ← standard Fabric installer
  <game_dir>/<mc_version>/.fabric/remapped-jars/...
  <game_dir>/.fabric/remapped-jars/...
"""

import json
import logging
import os
import platform
import subprocess
from pathlib import Path

MAVEN_REPOS = [
    "https://libraries.minecraft.net/", # Mojang
    "https://maven.fabricmc.net/",      # Fabric
    "https://repo1.maven.org/maven2/"   # Maven Central fallback
]

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("launcher")

MAIN_CLASS = "net.fabricmc.loader.impl.launch.knot.KnotClient"


# ── OS helpers ────────────────────────────────────────────────────────────────

def _os_name() -> str:
    s = platform.system().lower()
    if s == "darwin":  return "macos"
    if s == "windows": return "windows"
    return "linux"


# ── Java ──────────────────────────────────────────────────────────────────────

def find_java(game_dir: str) -> Path | None:
    base = Path(game_dir).resolve() / "java" / _os_name() / "bin"
    log.debug(f"Looking for Java in: {base}")
    for name in ("javaw.exe", "java.exe", "java"):
        candidate = base / name
        if candidate.exists():
            log.info(f"Found Java: {candidate}")
            return candidate
    log.error("No Java found")
    return None


# ── Fabric JAR ────────────────────────────────────────────────────────────────

def find_fabric_jar(game_dir: str, mc_version: str) -> Path | None:
    base = Path(game_dir).resolve()
    log.debug(f"Searching Fabric JAR in: {base} (mc_version={mc_version})")

    # 1. Standard Fabric installer location: versions/<mc_version>/*.jar
    versions_dir = base / "versions" / mc_version
    if versions_dir.is_dir():
        jars = list(versions_dir.glob("*.jar"))
        fabric_jars = [j for j in jars if "fabric" in j.name.lower()]
        chosen = fabric_jars[0] if fabric_jars else (jars[0] if jars else None)
        if chosen:
            log.info(f"Found JAR in versions dir: {chosen}")
            return chosen

    # 2. Legacy .fabric/remapped-jars search
    for candidate in (base, base / mc_version):
        for remap_dir in ("remapped-jars", "remappedJars"):
            root = candidate / ".fabric" / remap_dir
            if not root.is_dir():
                continue
            log.info(f"Found remap root: {root}")
            direct = root / "client-intermediary.jar"
            if direct.exists():
                return direct
            for child in root.iterdir():
                jar = child / "client-intermediary.jar"
                if jar.exists():
                    return jar

    log.error("Fabric JAR not found")
    return None


# ── Maven path helper ─────────────────────────────────────────────────────────

def maven_to_path(lib_name: str, base: Path) -> Path:
    parts = lib_name.split(":")
    if len(parts) < 3:
        log.error(f"Invalid maven name: {lib_name}")
        return base / "invalid"
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    group_path = Path(*group.split("."))
    filename = f"{artifact}-{version}{classifier}.jar"
    return base / group_path / artifact / version / filename


# ── Classpath builder ─────────────────────────────────────────────────────────

def _build_classpath(game_dir_path: Path, mc_version: str) -> list[str] | None:
    """
    Returns an ordered classpath list, or None if version JSON is missing.
    Order: libraries → fabric loader JAR → vanilla game JAR
    """
    classpath: list[str] = []
    lib_dir = game_dir_path / "libraries"

    # ── Libraries from version JSON ───────────────────────────────────────────
    version_json_path = game_dir_path / "versions" / mc_version / f"{mc_version}.json"
    if not version_json_path.exists():
        log.error(f"Missing version JSON: {version_json_path}")
        return None

    with open(version_json_path, "r", encoding="utf-8") as f:
        version_data = json.load(f)

    libs = version_data.get("libraries", [])
    log.info(f"Libraries declared in JSON: {len(libs)}")

    for lib in libs:
        lib_file = None
        download_url = None

        # 1. Try Mojang artifact info
        artifact = lib.get("downloads", {}).get("artifact", {})
        if artifact.get("path"):
            lib_file = lib_dir / artifact["path"]
            download_url = artifact.get("url")

        # 2. Fallback to Maven coordinates
        if (not lib_file or not lib_file.exists()) and "name" in lib:
            maven_path = maven_to_path(lib["name"], Path("")) # Get relative path
            lib_file = lib_dir / maven_path
            
            # If we don't have a URL yet, try to build one from common repos
            if not download_url:
                # Convert Maven path to URL format (forward slashes)
                url_suffix = str(maven_path).replace("\\", "/")
                # For Fabric/other libs, we check our repo list
                for repo in MAVEN_REPOS:
                    test_url = repo + url_suffix
                    # We'll try to download this in the next step
                    download_url = test_url 

        # ── THE DOWNLOAD LOGIC ──
        if lib_file and not lib_file.exists():
            if download_url:
                log.info(f"Library missing, attempting download: {lib.get('name')}")
                success = download_file(download_url, lib_file)
                if not success:
                    log.warning(f"Could not download {lib.get('name')} from {download_url}")
            else:
                log.warning(f"Missing lib {lib.get('name')} and no URL found to download it.")

        if lib_file and lib_file.exists():
            classpath.append(str(lib_file))

    # ── Fabric loader JAR (contains KnotClient) ───────────────────────────────
    fabric_jar = find_fabric_jar(str(game_dir_path), mc_version)
    if fabric_jar:
        if str(fabric_jar) not in classpath:
            classpath.append(str(fabric_jar))
            log.info(f"Appended Fabric loader JAR: {fabric_jar}")
    else:
        log.error("Fabric loader JAR NOT found — KnotClient will be missing!")

    # ── Vanilla game JAR (must be last) ───────────────────────────────────────
    vanilla_jar = game_dir_path / "versions" / mc_version / f"{mc_version}.jar"
    if vanilla_jar.exists():
        if str(vanilla_jar) not in classpath:
            classpath.append(str(vanilla_jar))
            log.info(f"Appended vanilla JAR: {vanilla_jar}")
    else:
        log.warning(f"Vanilla JAR not found at: {vanilla_jar} (may be fine if Fabric replaces it)")

    log.info(f"Classpath: {len(classpath)} entries")
    return classpath


# ── Launch command ────────────────────────────────────────────────────────────

def build_launch_command(
    game_dir: str,
    mc_version: str,
    jvm_mb: int = 2048,
    username: str = "Player",
    access_token: str = "0",
    uuid: str = "00000000-0000-0000-0000-000000000000",
) -> list[str] | None:

    java = find_java(game_dir)
    if java is None:
        log.error("Aborting: Java not found")
        return None

    game_dir_path = Path(game_dir).resolve()

    classpath = _build_classpath(game_dir_path, mc_version)
    if classpath is None:
        return None
    if not classpath:
        log.error("Classpath is empty — aborting")
        return None

    cp_string = os.pathsep.join(classpath)

    cmd = [
        str(java),
        f"-Xmx{jvm_mb}M",
        f"-Xms{max(512, jvm_mb // 4)}M",
        "-XX:+UseG1GC",
        "-Djava.library.path=" + str(game_dir_path / "natives"),
        "-cp", cp_string,
        MAIN_CLASS,
        "--gameDir",   str(game_dir_path),
        "--assetsDir", str(game_dir_path / "assets"),
        "--assetIndex", mc_version,
        "--version",   mc_version,
        "--username",  username,
        "--uuid",      uuid,
        "--accessToken", access_token,
        "--userType",  "msa" if access_token != "0" else "legacy",
    ]

    log.debug("Launch command:")
    for part in cmd:
        log.debug(f"  {part}")

    return cmd


# ── Public launch entry point ─────────────────────────────────────────────────

def launch(
    game_dir: str,
    mc_version: str,
    jvm_mb: int = 2048,
    username: str = "Player",
    access_token: str = "0",
    uuid: str = "00000000-0000-0000-0000-000000000000",
) -> tuple[str | None, subprocess.Popen | None]:
    """
    Launch the game in a detached process.
    Returns (None, process) on success, or (error_string, None) on failure.
    """
    cmd = build_launch_command(game_dir, mc_version, jvm_mb, username, access_token, uuid)
    if cmd is None:
        if find_java(game_dir) is None:
            return "Bundled Java not found. Check game_dir/java/<os>/bin/", None
        return f"Could not build launch command for {mc_version} — check logs.", None

    kwargs: dict = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    cwd      = str(Path(game_dir).resolve())
    log_path = Path(game_dir) / "launcher_debug.log"

    with open(log_path, "w") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log_file,
            stderr=log_file,
            **kwargs,
        )

    log.info(f"Game launched (PID={process.pid}). Output → {log_path}")
    return None, process

import urllib.request
def download_file(url: str, dest: Path):
    """Downloads a file to the specified path, creating directories if needed."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"Downloading: {url} -> {dest}")
        with urllib.request.urlopen(url) as response, open(dest, "wb") as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        log.error(f"Failed to download {url}: {e}")
        return False
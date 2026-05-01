"""
core/game_launcher.py  –  Locate Java and launch Fabric/Minecraft.

Java lookup (in order):
  1. Bundled: <game_dir>/java/<os>/bin/java   (or javaw.exe on Windows)
  2. System: JAVA_HOME or PATH (with macOS /usr/libexec/java_home support)

Fabric JAR location (checked in order):
  <game_dir>/versions/<mc_version>/<name>.jar   ← standard Fabric installer
  <game_dir>/<mc_version>/.fabric/remapped-jars/...
  <game_dir>/.fabric/remapped-jars/...
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path

# -----------------------------------------------------------------------------
#  Logging setup
# -----------------------------------------------------------------------------
log = logging.getLogger("launcher")

MAVEN_REPOS = [
    "https://libraries.minecraft.net/",   # Mojang
    "https://maven.fabricmc.net/",        # Fabric
    "https://repo1.maven.org/maven2/",    # Maven Central fallback
]

MAIN_CLASS = "net.fabricmc.loader.impl.launch.knot.KnotClient"


# -----------------------------------------------------------------------------
#  OS helpers
# -----------------------------------------------------------------------------
def _os_name() -> str:
    """Returns 'windows', 'macos', or 'linux'."""
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    return "linux"


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


# -----------------------------------------------------------------------------
#  Java detection (bundled first, then system)
# -----------------------------------------------------------------------------
def find_java(game_dir: str) -> Path | None:
    """
    Locates a usable Java executable.
    1. Looks for bundled Java: <game_dir>/java/<os>/bin/java (or javaw.exe on Windows)
    2. Falls back to system Java (JAVA_HOME or PATH)
    Returns a Path object or None.
    """
    base = Path(game_dir).resolve() / "java" / _os_name() / "bin"
    log.debug(f"Looking for bundled Java in: {base}")

    # Executable names: Windows prefers javaw.exe (no console), others use "java"
    exe_names = ["javaw.exe", "java.exe"] if _is_windows() else ["java"]

    for name in exe_names:
        candidate = base / name
        if candidate.exists():
            log.info(f"Found bundled Java: {candidate}")
            return candidate

    log.info("Bundled Java not found – falling back to system Java")

    # System Java fallback
    # 1. Check JAVA_HOME environment variable
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        java_exe = Path(java_home) / "bin" / ("javaw.exe" if _is_windows() else "java")
        if java_exe.exists():
            log.info(f"Found Java via JAVA_HOME: {java_exe}")
            return java_exe

    # 2. Check PATH via shutil.which()
    system_java = shutil.which("javaw.exe" if _is_windows() else "java")
    if system_java:
        log.info(f"Found Java on PATH: {system_java}")
        return Path(system_java)

    # 3. macOS specific: use /usr/libexec/java_home
    if platform.system() == "Darwin":
        try:
            java_home_path = subprocess.check_output(
                ["/usr/libexec/java_home"], text=True
            ).strip()
            java_exe = Path(java_home_path) / "bin" / "java"
            if java_exe.exists():
                log.info(f"Found macOS Java via java_home: {java_exe}")
                return java_exe
        except Exception as e:
            log.warning(f"Failed to run /usr/libexec/java_home: {e}")

    log.error("No Java found (neither bundled nor system)")
    return None


# -----------------------------------------------------------------------------
#  Fabric JAR locator
# -----------------------------------------------------------------------------
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
            log.debug(f"Checking remap root: {root}")
            direct = root / "client-intermediary.jar"
            if direct.exists():
                log.info(f"Found Fabric JAR: {direct}")
                return direct
            for child in root.iterdir():
                jar = child / "client-intermediary.jar"
                if jar.exists():
                    log.info(f"Found Fabric JAR: {jar}")
                    return jar

    log.error("Fabric JAR not found")
    return None


# -----------------------------------------------------------------------------
#  Maven helper and file download
# -----------------------------------------------------------------------------
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


def download_file(url: str, dest: Path) -> bool:
    """Download a file to the specified path, creating directories as needed."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"Downloading: {url} -> {dest}")
        with urllib.request.urlopen(url) as response, open(dest, "wb") as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        log.error(f"Failed to download {url}: {e}")
        return False


# -----------------------------------------------------------------------------
#  Classpath builder (with library downloading from Maven)
# -----------------------------------------------------------------------------
def _build_classpath(game_dir_path: Path, mc_version: str) -> list[str] | None:
    """
    Returns an ordered classpath list, or None if version JSON is missing.
    Order: libraries → fabric loader JAR → vanilla game JAR
    """
    classpath: list[str] = []
    lib_dir = game_dir_path / "libraries"

    # ---- Libraries from version JSON ------------------------------------
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
            maven_path = maven_to_path(lib["name"], Path(""))
            lib_file = lib_dir / maven_path

            if not download_url:
                # Convert Maven path to URL format
                url_suffix = str(maven_path).replace("\\", "/")
                for repo in MAVEN_REPOS:
                    test_url = repo + url_suffix
                    # We'll try to download from this URL
                    download_url = test_url
                    # Break after first candidate; actual download may still fail
                    break

        # Download missing library
        if lib_file and not lib_file.exists():
            if download_url:
                log.info(f"Library missing, downloading: {lib.get('name')}")
                success = download_file(download_url, lib_file)
                if not success:
                    log.warning(f"Could not download {lib.get('name')} from {download_url}")
            else:
                log.warning(f"Missing lib {lib.get('name')} and no URL to download it.")

        if lib_file and lib_file.exists():
            classpath.append(str(lib_file))

    # ---- Fabric loader JAR (contains KnotClient) -------------------------
    fabric_jar = find_fabric_jar(str(game_dir_path), mc_version)
    if fabric_jar:
        if str(fabric_jar) not in classpath:
            classpath.append(str(fabric_jar))
            log.info(f"Appended Fabric loader JAR: {fabric_jar}")
    else:
        log.error("Fabric loader JAR NOT found — KnotClient will be missing!")

    # ---- Vanilla game JAR (must be last) ---------------------------------
    vanilla_jar = game_dir_path / "versions" / mc_version / f"{mc_version}.jar"
    if vanilla_jar.exists():
        if str(vanilla_jar) not in classpath:
            classpath.append(str(vanilla_jar))
            log.info(f"Appended vanilla JAR: {vanilla_jar}")
    else:
        log.warning(f"Vanilla JAR not found at: {vanilla_jar} (may be fine if Fabric replaces it)")

    log.info(f"Classpath contains {len(classpath)} entries")
    return classpath


# -----------------------------------------------------------------------------
#  Launch command builder
# -----------------------------------------------------------------------------
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

    if platform.system() == "Darwin":   # macOS
        cmd.append("-XstartOnFirstThread")

    log.debug("Launch command:")
    for part in cmd:
        log.debug(f"  {part}")

    return cmd


# -----------------------------------------------------------------------------
#  Public launch function
# -----------------------------------------------------------------------------
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
            return "Java not found (bundled or system). Please install Java 17+ or place bundled Java in game_dir/java/<os>/bin/", None
        return f"Could not build launch command for {mc_version} — check logs.", None

    # Detach the process (platform-specific)
    kwargs: dict = {}
    if platform.system() == "Windows":
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

    log.info(f"Game launched (PID={process.pid}). Output → {log_path}")
    return None, process
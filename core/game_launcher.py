"""
core/game_launcher.py  –  Locate bundled Java and launch Fabric/Minecraft.

Java layout:
  <game_dir>/java/windows/bin/java.exe  (Windows – no console window)
  <game_dir>/java/linux/bin/java
  <game_dir>/java/macos/bin/java

Fabric JAR location:
  <game_dir>/<mc_version>/.fabric/remapped-jars/<any-dir>/client-intermediary.jar
"""

import logging

logging.basicConfig(
    level=logging.DEBUG,  # change to INFO later
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("launcher")


import os
import platform
import subprocess
import sys
from pathlib import Path


def _os_name() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    return "linux"


def find_java(game_dir: str) -> Path | None:
    base = Path(game_dir).resolve() / "java" / _os_name() / "bin"
    log.debug(f"Looking for Java in: {base}")

    for name in ("javaw.exe", "java.exe", "java"):
        candidate = base / name
        log.debug(f"Checking: {candidate}")
        if candidate.exists():
            log.info(f"Found Java: {candidate}")
            return candidate

    log.error("No Java found")
    return None


def find_fabric_jar(game_dir: str, mc_version: str) -> Path | None:
    base = Path(game_dir).resolve()
    log.debug(f"Searching Fabric JAR in: {base} (mc_version={mc_version})")

    candidates = [base, base / mc_version]

    for candidate in candidates:
        for remap_dir in ("remapped-jars", "remappedJars"):
            candidate_root = candidate / ".fabric" / remap_dir
            log.debug(f"Checking remap dir: {candidate_root}")

            if candidate_root.is_dir():
                log.info(f"Found remap root: {candidate_root}")

                direct = candidate_root / "client-intermediary.jar"
                if direct.exists():
                    log.info(f"Found direct jar: {direct}")
                    return direct

                for child in candidate_root.iterdir():
                    jar = child / "client-intermediary.jar"
                    log.debug(f"Checking child: {jar}")
                    if jar.exists():
                        log.info(f"Found jar: {jar}")
                        return jar

    log.error("Fabric JAR not found")
    return None


import json

def _read_version_json(game_dir: Path, mc_version: str) -> dict | None:
    vdir = game_dir / "versions" / mc_version
    vjson = vdir / f"{mc_version}.json"
    if vjson.exists():
        with open(vjson, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _build_classpath(game_dir: Path, version_data: dict) -> list[str]:
    cp = []

    # Add libraries in order
    for lib in version_data.get("libraries", []):
        artifact = lib.get("downloads", {}).get("artifact", {})
        path = artifact.get("path")
        if path:
            lib_path = game_dir / "libraries" / path
            if lib_path.exists():
                cp.append(str(lib_path))

    # Add main jar LAST (important)
    main_jar = game_dir / "versions" / version_data["id"] / f"{version_data['id']}.jar"
    if main_jar.exists():
        cp.append(str(main_jar))

    return cp


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
    log.debug(f"Game dir: {game_dir_path}")

    jar_path = game_dir_path / "versions" / mc_version / f"{mc_version}.jar"
    log.debug(f"Primary jar path: {jar_path}")

    if not jar_path.exists():
        log.warning("Primary jar missing, trying Fabric search")
        jar_path = find_fabric_jar(game_dir, mc_version)

    if jar_path is None or not jar_path.exists():
        log.error("No valid JAR found")
        return None

    log.info(f"Using JAR: {jar_path}")

    classpath = []
    lib_dir = game_dir_path / "libraries"

    if lib_dir.exists():
        version_json_path = game_dir_path / "versions" / mc_version / f"{mc_version}.json"
        
        if not version_json_path.exists():
            log.error(f"Missing version json: {version_json_path}")
            return None

        with open(version_json_path, "r", encoding="utf-8") as f:
            version_data = json.load(f)

        libs = version_data.get("libraries", [])
        log.info(f"Libraries declared in JSON: {len(libs)}")

        slf4j_found = False
    
    for lib in libs:
        lib_file = None
        
        # 1. Check for standard Mojang path
        artifact = lib.get("downloads", {}).get("artifact", {})
        if artifact.get("path"):
            lib_file = lib_dir / artifact["path"]
        
        # 2. Check for Maven name (crucial for Fabric/Loader libraries)
        if (not lib_file or not lib_file.exists()) and "name" in lib:
            lib_file = maven_to_path(lib["name"], lib_dir)

        if lib_file and lib_file.exists():
            classpath.append(str(lib_file))
            if "slf4j-api" in str(lib_file):
                slf4j_found = True
        else:
            # THIS IS THE CULPRIT: 
            # If this logs, you found the file that is missing!
            log.warning(f"CRITICAL MISSING LIB: {lib.get('name')} -> Expected at: {lib_file}")

    if not slf4j_found:
        log.error("SYSTEM ERROR: slf4j-api was not found in the version.json libraries list!")

    else:
        log.warning("Libraries folder missing")

    # Re-append the main/Fabric JAR at the very end
    classpath.append(str(jar_path))

    cp_string = os.pathsep.join(classpath)
    log.debug(f"Classpath length: {len(classpath)} entries")

    cmd = [
        str(java),
        f"-Xmx{jvm_mb}M",
        f"-Xms{max(512, jvm_mb // 4)}M",
        "-XX:+UseG1GC",
        "-Djava.library.path=" + str(game_dir_path / "natives"),
        "-cp", cp_string,
        "net.fabricmc.loader.impl.launch.knot.KnotClient",
        "--gameDir", str(game_dir_path),
        "--assetsDir", str(game_dir_path / "assets"),
        "--assetIndex", mc_version,
        "--version", mc_version,
        "--username", username,
        "--uuid", uuid,
        "--accessToken", access_token,
        "--userType", "msa" if access_token != "0" else "legacy"
    ]

    log.debug("Launch command:")
    for part in cmd:
        log.debug(f"  {part}")

    return cmd

def maven_to_path(lib_name: str, base: Path) -> Path:
    # Handles group:artifact:version[:classifier]
    parts = lib_name.split(":")
    if len(parts) < 3:
        log.error(f"Invalid maven name: {lib_name}")
        return base / "invalid"

    group = parts[0]
    artifact = parts[1]
    version = parts[2]
    
    # If there's a 4th part, it's a classifier (e.g., 'natives-windows')
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    
    group_path = Path(*group.split("."))
    filename = f"{artifact}-{version}{classifier}.jar"
    
    return base / group_path / artifact / version / filename


def launch(game_dir: str, mc_version: str, jvm_mb: int = 2048,
           username: str = "Player", access_token: str = "0",
           uuid: str = "00000000-0000-0000-0000-000000000000") -> str | None:
    """
    Launch the game in a detached process.
    Returns None on success or an error string.
    """
    cmd = build_launch_command(game_dir, mc_version, jvm_mb,
                                username, access_token, uuid)
    if cmd is None:
        if find_java(game_dir) is None:
            return "Bundled Java not found. Check game_dir/java/<os>/bin/"
        return f"Fabric JAR not found in {game_dir}/{mc_version}/.fabric/remapped-jars/"

    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    cwd = str(Path(game_dir).resolve())
    log_path = Path(game_dir) / "launcher_debug.log"
    with open(log_path, "w") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log_file,  # Redirect stdout to file
            stderr=log_file,  # Redirect stderr to file
            **kwargs
        )
    log.info(f"Game launched. If it crashes, check {log_path}")

    log.info(f"Launched process PID={process.pid}")

    # Stream output live
    #for line in process.stdout:
        #log.info(f"[JAVA STDOUT] {line.strip()}")

    #for line in process.stderr:
        #log.error(f"[JAVA STDERR] {line.strip()}")
    return None
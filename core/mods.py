"""
core/mods.py  –  Fabric mod management.
Mods live in <game_dir>/mods/.
Enabled:  <name>.jar
Disabled: <name>.jar.disabled
Tries to read fabric.mod.json inside each JAR for human-readable name/icon.
"""
import json
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
try:
    from PIL import Image
    import io
    _PIL = True
except ImportError:
    _PIL = False
@dataclass
class Mod:
    filename: str       # e.g. "sodium-0.5.jar"
    enabled:  bool
    name:     str       # display name from fabric.mod.json, fallback = filename stem
    version:  str = ""
    description: str = ""
    icon_data: Optional[bytes] = field(default=None, repr=False)  # raw PNG bytes
def _read_mod_meta(jar_path: Path) -> dict:
    """Extract fabric.mod.json from the JAR."""
    try:
        with zipfile.ZipFile(jar_path, "r") as z:
            if "fabric.mod.json" in z.namelist():
                return json.loads(z.read("fabric.mod.json"))
    except Exception:
        pass
    return {}
def _read_icon(jar_path: Path, meta: dict) -> Optional[bytes]:
    if not _PIL:
        return None
    icon_path = meta.get("icon", "")
    if not icon_path:
        return None
    try:
        with zipfile.ZipFile(jar_path, "r") as z:
            if icon_path in z.namelist():
                raw = z.read(icon_path)
                img = Image.open(io.BytesIO(raw)).convert("RGBA").resize((32, 32), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
    except Exception:
        pass
    return None
def list_mods(game_dir: str) -> list:
    mods_dir = Path(game_dir) / "mods"
    if not mods_dir.is_dir():
        return []
    mods = []
    for entry in sorted(mods_dir.iterdir()):
        name = entry.name
        if name.endswith(".jar"):
            enabled = True
            jar = entry
        elif name.endswith(".jar.disabled"):
            enabled = False
            jar = entry
        else:
            continue
        meta = _read_mod_meta(jar)
        display_name = meta.get("name") or jar.stem.split("-")[0].capitalize()
        version      = meta.get("version", "")
        desc         = meta.get("description", "")
        icon         = _read_icon(jar, meta)
        mods.append(Mod(
            filename=name,
            enabled=enabled,
            name=display_name,
            version=version,
            description=desc,
            icon_data=icon,
        ))
    return mods
def toggle_mod(game_dir: str, filename: str) -> bool:
    """Toggle a mod on/off. Returns new enabled state."""
    mods_dir = Path(game_dir) / "mods"
    path = mods_dir / filename
    if filename.endswith(".jar.disabled"):
        new_path = mods_dir / filename[:-len(".disabled")]
        path.rename(new_path)
        return True
    elif filename.endswith(".jar"):
        new_path = mods_dir / (filename + ".disabled")
        path.rename(new_path)
        return False
    return False
def open_mods_folder(game_dir: str):
    """Open the mods folder in the OS file manager."""
    import subprocess
    import platform
    folder = str(Path(game_dir) / "mods")
    Path(folder).mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Windows":
        os.startfile(folder)
    elif system == "Darwin":
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])
def open_folder(game_dir: str, subfolder: str):
    """Open any subfolder in the OS file manager."""
    import subprocess
    import platform
    folder = str(Path(game_dir) / subfolder)
    Path(folder).mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Windows":
        os.startfile(folder)
    elif system == "Darwin":
        subprocess.Popen(["open", folder])
    else:
        # xdg-open can fail on bare WMs with no DBus file manager registered.
        # Fall back through common file managers instead
        file_managers = ["thunar", "nautilus", "dolphin", "nemo", "pcmanfm", "ranger"]
        for fm in file_managers:
            try:
                result = subprocess.run(
                    ["which", fm], capture_output=True, text=True
                )
                if result.returncode == 0: # which returns 0 if the file manager is installed
                    subprocess.Popen([fm, folder])
                    return
            except Exception:
                continue
        # If no file managers found, use the appropriate terminal instead
        for term in ["gnome-terminal", "alacritty", "kitty", "xterm", "foot"]:
            try:
                result = subprocess.run(
                    ["which", term], capture_output=True, text=True
                )
                if result.returncode == 0:
                    subprocess.Popen([term, "--working-directory", folder])
                    return
            except Exception:
                continue

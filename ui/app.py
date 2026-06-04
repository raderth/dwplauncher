"""
ui/app.py  –  DWP Launcher using pywebview.
"""
import base64
import json
import os
import threading
import sys
import webbrowser
import secrets
import subprocess
from pathlib import Path

import requests
import webview

from core import config, version
from core.installer import Installer
from core.game_launcher import launch, find_java
from core.mods import list_mods, toggle_mod, open_folder
from core.auth import login_microsoft

def _find_logo(game_dir: str) -> str:
    """Load logo.png and convert to data URI, or return empty string."""
    possible_paths = [
        Path(game_dir).parent / "logo.png",
        Path("./logo.png"),
        Path(__file__).parent.parent / "logo.png",
    ]
    
    for logo_path in possible_paths:
        if logo_path.exists():
            try:
                with open(logo_path, "rb") as f:
                    data = base64.b64encode(f.read()).decode('utf-8')
                print(f"Loaded logo from: {logo_path}")
                return f"data:image/png;base64,{data}"
            except Exception as e:
                print(f"Failed to load logo from {logo_path}: {e}")
                continue
    
    print("No logo.png found in expected locations")
    return ""

def choose_java(self):
    """Open a file dialog to pick a Java executable."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    filetypes = [
        ("Java executable", "java*"),
        ("All files", "*"),
    ]
    path = filedialog.askopenfilename(
        title="Select Java Executable (java or javaw)",
        filetypes=filetypes,
    )
    root.destroy()
    if path:
        self._cfg["java_path"] = path
        config.save(self._cfg)
        self._js(f'_onJavaPathChosen({json.dumps(path)})')
    else:
        self._js('_onJavaPathChosen("")')

def clear_java_path(self):
    """Remove custom Java and revert to auto-detection."""
    self._cfg["java_path"] = None
    config.save(self._cfg)
    self._js('_onJavaPathCleared()')



def _build_html(logo_data_uri: str) -> str:
    logo_html = (
        f'<img id="logo-img" src="{logo_data_uri}" alt="DWP Logo" crossorigin="anonymous">'
        if logo_data_uri else
        '<div id="logo-text">DWP</div>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DWP Launcher</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=Inter:wght@400;500;600&display=swap');

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:       #0a0a0a;
    --bg2:      #111111;
    --bg3:      #1a1a1a;
    --bg4:      #222222;
    --crimson:  #8b1a2f;
    --crimson2: #b02040;
    --green:    #3ab04a;
    --green2:   #2d8a3a;
    --border:   #2a2a2a;
    --text:     #f0f0f0;
    --text2:    #999999;
    --text3:    #555555;
    --radius:   8px;
    --dock-h:   52px;
  }}

  html, body {{
    width: 100%; height: 100%;
    display: flex; flex-direction: column;
    margin: 0; padding: 0;
    background: var(--bg); color: var(--text);
    font-family: 'Inter', sans-serif; font-size: 13px;
    overflow: hidden;
    user-select: none; -webkit-user-select: none;
  }}

  #drag-strip {{
    flex: 0 0 28px;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding: 2px 8px 0 0;
    z-index: 200;
    user-select: none;
    -webkit-user-select: none;
    cursor: grab;
  }}
  #drag-strip:active {{ cursor: grabbing; }}
  #drag-area {{
    flex: 1;
    min-height: 28px;
    cursor: inherit;
  }}
  #win-controls {{
    display: flex;
    gap: 4px;
    flex-shrink: 0;
    cursor: default;
  }}
  .win-btn {{
    width: 24px; height: 18px;
    display: flex; align-items: center; justify-content: center;
    border-radius: 4px; cursor: pointer;
    color: var(--text3); font-size: 10px;
    transition: background 0.15s, color 0.15s;
  }}
  .win-btn:hover {{ background: var(--bg4); color: var(--text2); }}
  #btn-close:hover {{ background: #c0293f; color: #fff; }}

  /* ── Content & Pages ── */
  #content {{
    flex: 1;
    position: relative;
    display: flex;
    overflow: hidden;
    min-width: 0;
  }}
  .page {{
    position: absolute; inset: 0;
    display: none;
    -webkit-app-region: no-drag;
    app-region: no-drag;
    width: 100%;
  }}
  .page.active {{ display: flex; flex-direction: column; }}

  /* ── PLAY ── */
  #page-play {{
    align-items: center; justify-content: center;
    background: var(--bg);
  }}
  #play-glow {{
    position: absolute; bottom: -80px; left: -80px;
    width: 520px; height: 420px;
    background: radial-gradient(ellipse at 30% 70%,
      rgba(139,26,47,0.75) 0%, rgba(100,10,30,0.40) 35%,
      rgba(30,5,10,0.15) 65%, transparent 80%);
    pointer-events: none; z-index: 0; filter: blur(8px);
  }}
  #play-inner {{
    position: relative; z-index: 1;
    display: flex; flex-direction: column; align-items: center;
    padding-bottom: calc(var(--dock-h) + 24px);
  }}
  #logo-ring {{
    width: 120px; height: 120px;
    position: relative; margin-bottom: 20px; flex-shrink: 0;
  }}
  #logo-canvas {{
    position: absolute; inset: 0; z-index: 2;
  }}
  #logo-text {{
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Rajdhani', sans-serif; font-size: 28px; font-weight: 700;
    color: var(--text); z-index: 2;
  }}
  #logo-img {{ 
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    object-fit: contain;
    z-index: 2;
    border-radius: 50%;
  }}

  #version-label {{ font-size: 11px; color: var(--text3); margin-bottom: 28px; }}
  #play-btn {{
    width: 220px; height: 54px;
    background: var(--crimson); border: 1.5px solid var(--crimson2);
    border-radius: var(--radius); color: var(--text);
    font-family: 'Rajdhani', sans-serif; font-size: 22px; font-weight: 700;
    letter-spacing: 3px; cursor: pointer;
    transition: background 0.15s, transform 0.1s;
    position: relative; overflow: hidden;
  }}
  #play-btn:hover:not(:disabled) {{ background: var(--crimson2); }}
  #play-btn:active:not(:disabled) {{ transform: scale(0.98); }}
  #play-btn:disabled {{ opacity: 0.6; cursor: default; }}
  .btn-sub {{
    display: block; font-size: 10px; font-family: 'Inter', sans-serif;
    font-weight: 500; letter-spacing: 1px; opacity: 0.75; line-height: 1;
  }}
  #btn-progress {{
    position: absolute; bottom: 0; left: 0; height: 3px;
    background: var(--green); width: 0%;
    transition: width 0.3s ease; border-radius: 0 0 var(--radius) 0;
  }}
  #status-label {{
    margin-top: 14px; font-size: 10px; color: var(--text3);
    max-width: 340px; text-align: center; min-height: 16px;
  }}

  /* ── MODS ── */
  /* FIX: ensure mods page and its children stretch to full width */
  #page-mods {{
    background: var(--bg);
    position: relative;
    width: 100%;
  }}
  #mods-glow {{
    position: absolute; bottom: -60px; left: -60px;
    width: 400px; height: 340px;
    background: radial-gradient(ellipse at 25% 75%,
      rgba(139,26,47,0.55) 0%, rgba(80,8,20,0.25) 40%, transparent 70%);
    pointer-events: none; z-index: 0; filter: blur(6px);
  }}
  #mods-content {{
    position: relative; z-index: 1;
    display: flex; flex-direction: column;
    padding: 16px; padding-bottom: calc(var(--dock-h) + 20px); gap: 12px;
    height: 100%; overflow-y: auto; flex: 1;
    width: 100%;            /* FIX: fill horizontal space */
    min-width: 0;           /* FIX: prevent flex blowout */
  }}
  #mods-header {{ display: flex; align-items: center; justify-content: space-between; width: 100%; }}
  #mods-header h2 {{
    font-family: 'Rajdhani', sans-serif; font-size: 18px;
    font-weight: 700; letter-spacing: 1px; color: var(--text);
  }}
  .mods-header-actions {{ display: flex; gap: 8px; }}
  .mods-action-btn {{
    padding: 5px 12px; background: var(--bg3);
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--text2); font-size: 11px; font-weight: 500;
    cursor: pointer; transition: all 0.15s;
  }}
  .mods-action-btn:hover {{ color: var(--text); border-color: var(--text3); }}
  #mods-grid-wrap {{
    flex: 1;
    width: 100%;            /* FIX: fill full width */
    min-width: 0;           /* FIX: prevent overflow */
    background: rgba(20,20,20,0.85);
    border: 1px solid var(--border); border-radius: 10px;
    overflow-y: auto; padding: 14px;
    min-height: 0;
  }}
  #mods-grid-wrap::-webkit-scrollbar {{ width: 5px; }}
  #mods-grid-wrap::-webkit-scrollbar-track {{ background: transparent; }}
  #mods-grid-wrap::-webkit-scrollbar-thumb {{ background: var(--bg4); border-radius: 3px; }}
  #mods-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 10px;
    width: 100%;            /* FIX: fill grid container */
  }}
  .mod-card {{
    background: var(--bg3); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 12px;
    display: flex; align-items: center; gap: 10px; transition: border-color 0.15s;
  }}
  .mod-card:hover {{ border-color: var(--text3); }}
  .mod-icon {{
    width: 36px; height: 36px; border-radius: 6px; background: var(--green2);
    flex-shrink: 0; display: flex; align-items: center; justify-content: center;
    font-size: 18px; overflow: hidden;
  }}
  .mod-icon img {{ width: 100%; height: 100%; object-fit: cover; border-radius: 6px; }}
  .mod-info {{ flex: 1; min-width: 0; }}
  .mod-name {{ font-size: 12px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .mod-version {{ font-size: 10px; color: var(--text3); margin-top: 1px; }}
  .mod-toggle {{
    width: 34px; height: 18px; border-radius: 9px; border: none;
    cursor: pointer; position: relative; flex-shrink: 0; transition: background 0.2s;
  }}
  .mod-toggle.on  {{ background: var(--green); }}
  .mod-toggle.off {{ background: var(--bg4); }}
  .mod-toggle::after {{
    content: ''; position: absolute; top: 2px;
    width: 14px; height: 14px; border-radius: 50%;
    background: white; transition: left 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.4);
  }}
  .mod-toggle.on::after  {{ left: 18px; }}
  .mod-toggle.off::after {{ left: 2px; }}
  #mods-empty {{ padding: 40px; text-align: center; color: var(--text3); font-size: 12px; }}

  /* ── SETTINGS ── */
  #page-settings {{ background: var(--bg); overflow-y: auto; }}
  #page-settings::-webkit-scrollbar {{ width: 5px; }}
  #page-settings::-webkit-scrollbar-thumb {{ background: var(--bg4); border-radius: 3px; }}
  #settings-inner {{
    padding: 20px 28px;
    padding-bottom: calc(var(--dock-h) + 32px);
    max-width: 560px; display: flex; flex-direction: column; gap: 24px;
  }}
  .settings-section {{ display: flex; flex-direction: column; gap: 12px; }}
  .settings-section-title {{
    font-family: 'Rajdhani', sans-serif; font-size: 15px; font-weight: 700;
    letter-spacing: 1px; color: var(--text);
    border-bottom: 1px solid var(--border); padding-bottom: 6px; margin-bottom: 2px;
  }}
  .settings-row {{ display: flex; align-items: center; gap: 12px; }}
  .settings-label {{ width: 130px; font-size: 12px; color: var(--text2); flex-shrink: 0; }}
  .settings-control {{ flex: 1; display: flex; align-items: center; gap: 10px; }}
  input[type="range"] {{
    flex: 1; height: 4px; -webkit-appearance: none; background: var(--bg4);
    border-radius: 2px; outline: none;
  }}
  input[type="range"]::-webkit-slider-thumb {{
    -webkit-appearance: none; width: 14px; height: 14px;
    border-radius: 50%; background: var(--crimson2); cursor: pointer;
  }}
  .range-val {{ font-size: 11px; color: var(--text2); min-width: 48px; text-align: right; }}
  .settings-input {{
    flex: 1; background: var(--bg3); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); padding: 6px 10px;
    font-family: 'Inter', sans-serif; font-size: 12px;
    outline: none; transition: border-color 0.15s;
  }}
  .settings-input:focus {{ border-color: var(--text3); }}
  .settings-btn {{
    padding: 7px 16px; background: var(--crimson); border: none; border-radius: 6px;
    color: var(--text); font-family: 'Inter', sans-serif;
    font-size: 12px; font-weight: 600; cursor: pointer;
    transition: background 0.15s; white-space: nowrap;
  }}
  .settings-btn:hover {{ background: var(--crimson2); }}
  .settings-btn.secondary {{
    background: var(--bg3); border: 1px solid var(--border); color: var(--text2);
  }}
  .settings-btn.secondary:hover {{ color: var(--text); border-color: var(--text3); }}
  .settings-btn.verified {{
    background: var(--green2); border: 1px solid var(--green); color: #fff; cursor: default;
  }}

  /* ── Floating dock ── */
  #dock {{
    position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%);
    z-index: 100; display: flex; align-items: center; gap: 2px;
    padding: 5px 6px;
    background: rgba(20,20,20,0.92);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 32px;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 8px 32px rgba(0,0,0,0.6), 0 1px 0 rgba(255,255,255,0.04) inset;
    -webkit-app-region: no-drag; app-region: no-drag;
  }}
  .dock-btn {{
    padding: 6px 16px; border: none; border-radius: 24px;
    background: transparent; color: var(--text3);
    font-family: 'Inter', sans-serif; font-size: 11px; font-weight: 600;
    letter-spacing: 0.4px; cursor: pointer;
    transition: background 0.15s, color 0.15s; white-space: nowrap;
  }}
  .dock-btn:hover  {{ background: rgba(255,255,255,0.06); color: var(--text2); }}
  .dock-btn.active {{ background: rgba(255,255,255,0.10); color: var(--text); }}
  .dock-sep {{
    width: 1px; height: 16px; background: rgba(255,255,255,0.08);
    margin: 0 2px; flex-shrink: 0;
  }}

  /* ── Toast ── */
  #toast {{
    position: fixed; bottom: 84px; left: 50%;
    transform: translateX(-50%) translateY(10px);
    background: var(--bg3); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 20px; font-size: 12px; color: var(--text2);
    opacity: 0; pointer-events: none; transition: opacity 0.2s, transform 0.2s; z-index: 999;
    -webkit-app-region: no-drag; app-region: no-drag;
  }}
  #toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}

  #auth-overlay {{
    display: none;
    position: fixed; inset: 0; z-index: 500;
    background: rgba(0,0,0,0.7);
    align-items: center; justify-content: center;
    backdrop-filter: blur(4px);
    -webkit-app-region: no-drag;
    app-region: no-drag;
  }}
  #auth-overlay.open {{ display: flex; }}
  #auth-popup {{
    width: 380px;
    background: #111; border: 1px solid #2a2a2a; border-radius: 14px;
    padding: 28px 26px; display: flex; flex-direction: column;
  }}
  #auth-popup h3 {{
    font-family: 'Rajdhani', sans-serif; font-size: 18px; font-weight: 700;
    margin-bottom: 12px; color: var(--text);
  }}
  #auth-code-input {{
    flex: 1; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 7px;
    color: var(--text); padding: 8px 12px;
    font-family: 'Inter', sans-serif; font-size: 13px; outline: none;
    margin-bottom: 4px;
  }}
  #auth-code-input:focus {{ border-color: #555; }}
  #auth-popup-msg.err {{ color: #c0293f; }}
  #auth-popup-msg.ok  {{ color: var(--green); }}
</style>
</head>
<body>

<!-- Drag strip -->
<div id="drag-strip">
  <div id="drag-area"></div>
  <div id="win-controls">
    <div class="win-btn" id="btn-min" onclick="pyapi('minimize')">–</div>
    <div class="win-btn" id="btn-close" onclick="pyapi('close')">✕</div>
  </div>
</div>

<!-- Content -->
<div id="content">

  <!-- PLAY page -->
  <div id="page-play" class="page active">
    <div id="play-glow"></div>
    <div id="play-inner">
      <div id="logo-ring">
        {logo_html}
        <canvas id="logo-canvas" width="120" height="120"></canvas>
      </div>
      <div id="version-label">Checking version…</div>
      <button id="play-btn" onclick="onPlayClick()">
        <span id="btn-main-label">PLAY</span>
        <span class="btn-sub" id="btn-sub-label"></span>
        <div id="btn-progress"></div>
      </button>
      <div id="status-label"></div>
    </div>
  </div>

  <!-- MODS page -->
  <div id="page-mods" class="page">
    <div id="mods-glow"></div>
    <div id="mods-content">
      <div id="mods-header">
        <h2>Installed Mods</h2>
        <div class="mods-header-actions">
          <button class="mods-action-btn" onclick="pyapi('open_folder','mods')">+ Add Mods</button>
          <button class="mods-action-btn" onclick="pyapi('open_folder','resourcepacks')">Resourcepacks</button>
          <button class="mods-action-btn" onclick="pyapi('open_folder','datapacks')">Datapacks</button>
        </div>
      </div>
      <div id="mods-grid-wrap">
        <div id="mods-grid"></div>
      </div>
    </div>
  </div>

  <!-- SETTINGS page -->
  <div id="page-settings" class="page">
    <div id="settings-inner">
      <div class="settings-section">
        <div class="settings-section-title">Performance</div>
        <div class="settings-row">
          <div class="settings-label">JVM Memory</div>
          <div class="settings-control">
            <input type="range" id="mem-slider" min="512" max="16384" step="256" value="2048"
                   oninput="updateMemLabel()">
            <span class="range-val" id="mem-label">2.0 GB</span>
          </div>
        </div>
        <div style="font-size:10px;color:var(--text3);padding-left:142px;" id="ram-hint"></div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Java Runtime</div>
        <div class="settings-row">
          <div class="settings-label">Java Version</div>
          <div class="settings-control" style="flex-wrap:wrap; gap:8px;">
            <select id="java-select" class="settings-input" style="flex:1; min-width:200px;"
                    onchange="onJavaSelectChange()">
              <option value="__loading__">Detecting…</option>
            </select>
          </div>
        </div>
        <div id="custom-java-row" class="settings-row" style="display:none;">
          <div class="settings-label">Custom Path</div>
          <div class="settings-control" style="gap:8px;">
            <input type="text" id="custom-java-input" class="settings-input"
                   placeholder="Paste full path to javaw.exe / java">
            <button class="settings-btn secondary" onclick="setCustomJava()">Set</button>
          </div>
        </div>
        <div style="font-size:10px;color:var(--text3);padding-left:142px;">
          Choose a specific Java installation or leave “Auto‑detect”.
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Account</div>
        <div class="settings-row">
          <div class="settings-label">Microsoft Login</div>
          <!-- Auth code popup -->
          <div id="auth-overlay">
            <div id="auth-popup">
              <h3>Microsoft Login</h3>
              <p style="font-size:11px;color:#888;margin-bottom:12px;">
                1. The browser has opened the Microsoft login page.<br>
                2. Sign in and you'll receive a <strong>code</strong>.<br>
                3. Paste that code below.
              </p>
              <input id="auth-code-input" type="text" placeholder="Paste code here…" autocomplete="off">
              <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
                <button class="settings-btn secondary" onclick="closeAuthPopup()">Cancel</button>
                <button class="settings-btn" onclick="submitAuthCode()">Verify</button>
              </div>
              <div id="auth-popup-msg" style="font-size:11px;margin-top:8px;"></div>
            </div>
          </div>
          <div class="settings-control">
            <button class="settings-btn secondary" id="login-btn" onclick="doLogin()">Login with Microsoft</button>
            <span id="login-status"></span>
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-label">Skin</div>
          <div class="settings-control">
            <button class="settings-btn secondary"
              onclick="pyapi('open_url','https://minecraft.net/profile/skin')">
              Open Skin Manager
            </button>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Installation</div>
        <div class="settings-row">
          <div class="settings-label">Integrity</div>
          <div class="settings-control">
            <button class="settings-btn" onclick="doVerifyRepair()">Verify &amp; Repair Files</button>
          </div>
        </div>
        <div style="font-size:10px;color:var(--text3);padding-left:142px;" id="installed-ver"></div>
      </div>

      <div class="settings-row" style="padding-top:8px;">
        <div class="settings-label"></div>
        <div class="settings-control">
          <button class="settings-btn" onclick="saveSettings()">Save Settings</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Dock -->
  <div id="dock">
    <button class="dock-btn active" data-tab="play"     onclick="showTab('play')">Play</button>
    <div class="dock-sep"></div>
    <button class="dock-btn"        data-tab="mods"     onclick="showTab('mods')">Mods</button>
    <div class="dock-sep"></div>
    <button class="dock-btn"        data-tab="settings" onclick="showTab('settings')">Settings</button>
  </div>

</div>

<!-- Toast -->
<div id="toast"></div>

<script>
// ── Window drag: absolute-position approach ───────────────────────────────────
let isDragging      = false;
let winStartX       = 0;
let winStartY       = 0;
let mouseStartX     = 0;
let mouseStartY     = 0;

const dragStrip = document.getElementById('drag-strip');

dragStrip.addEventListener('mousedown', async (e) => {{
  if (e.target.closest('#win-controls')) return;
  e.preventDefault();
  isDragging = false;

  const pos = await window.pywebview.api.get_window_pos();
  winStartX   = pos.x;
  winStartY   = pos.y;
  mouseStartX = e.screenX;
  mouseStartY = e.screenY;
  isDragging  = true;
  dragStrip.style.cursor = 'grabbing';
}});

document.addEventListener('mousemove', (e) => {{
  if (!isDragging || !window.pywebview || !window.pywebview.api) return;
  const newX = winStartX + (e.screenX - mouseStartX);
  const newY = winStartY + (e.screenY - mouseStartY);
  window.pywebview.api.move_window_abs(newX, newY);
}});

document.addEventListener('mouseup', () => {{
  isDragging = false;
  dragStrip.style.cursor = 'grab';
}});

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(tab) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.dock-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + tab).classList.add('active');
  document.querySelector('.dock-btn[data-tab="' + tab + '"]').classList.add('active');
  if (tab === 'mods') refreshMods();
}}

// ── Python bridge ─────────────────────────────────────────────────────────────
function pyapi(action, ...args) {{
  if (window.pywebview && window.pywebview.api) {{
    return window.pywebview.api[action](...args);
  }}
}}

// ── Play ──────────────────────────────────────────────────────────────────────
let btnState = 'idle';

function onPlayClick() {{
  if (btnState === 'idle' || btnState === 'error') pyapi('start_download');
  else if (btnState === 'ready') {{
    setBtnState('launching', 'LAUNCHING', 'starting…', 100);
    pyapi('launch_game');
  }}
  else if (btnState === 'running') pyapi('stop_game');
}}

function setBtnState(state, mainLabel, subLabel, progress) {{
  btnState = state;
  document.getElementById('btn-main-label').textContent = mainLabel || state.toUpperCase();
  document.getElementById('btn-sub-label').textContent  = subLabel  || '';
  document.getElementById('btn-progress').style.width   = (progress || 0) + '%';
  document.getElementById('play-btn').disabled =
    (state === 'downloading' || state === 'verifying' || state === 'launching');
}}

function setStatus(msg)  {{ document.getElementById('status-label').textContent  = msg; }}
function setVersion(msg) {{ document.getElementById('version-label').textContent = msg; }}
function setProgress(pct, mainLabel, subLabel) {{
  document.getElementById('btn-progress').style.width = pct + '%';
  if (mainLabel)              document.getElementById('btn-main-label').textContent = mainLabel;
  if (subLabel !== undefined) document.getElementById('btn-sub-label').textContent  = subLabel;
}}

// ── Logo pixel-scan ───────────────────────────────────────────────────────────
function renderLogo() {{
  const img = document.getElementById('logo-img');
  const text = document.getElementById('logo-text');
  if (!img || !img.src || img.src === window.location.href) return;
  img.style.display = 'block';
  if (text) text.style.display = 'none';
  const SIZE = 120;
  const canvas = document.getElementById('logo-canvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, SIZE, SIZE);
  ctx.drawImage(img, 0, 0, SIZE, SIZE);
}}

// ── Mods ──────────────────────────────────────────────────────────────────────
function refreshMods() {{
  const r = pyapi('get_mods');
  if (r && r.then) r.then(renderMods);
  else if (r) renderMods(r);
}}
function renderMods(mods) {{
  const grid = document.getElementById('mods-grid');
  if (!mods || mods.length === 0) {{
    grid.innerHTML = '<div id="mods-empty">No mods found in mods/ folder.</div>';
    return;
  }}
  grid.innerHTML = mods.map(mod => 
    `<div class="mod-card">
      <div class="mod-icon">
        ${{mod.icon_b64 ? `<img src="data:image/png;base64,${{mod.icon_b64}}" alt="">` : '🎮'}}
      </div>
      <div class="mod-info">
        <div class="mod-name">${{mod.name}}</div>
        <div class="mod-version">${{mod.version || ''}}</div>
      </div>
      <button class="mod-toggle ${{mod.enabled ? 'on' : 'off'}}"
              onclick="toggleMod('${{mod.filename}}')"></button>
    </div>`
  ).join('');
}}
function toggleMod(filename) {{
  const r = pyapi('toggle_mod', filename);
  if (r && r.then) r.then(() => refreshMods());
  else refreshMods();
}}

// ── Settings ──────────────────────────────────────────────────────────────────
function updateMemLabel() {{
  const mb = parseInt(document.getElementById('mem-slider').value);
  document.getElementById('mem-label').textContent = (mb / 1024).toFixed(1) + ' GB';
}}

async function doLogin() {{
  const btn = document.getElementById('login-btn');
  btn.textContent = 'Opening browser…';
  btn.disabled = true;
  const sessionId = await pyapi('start_auth');
  if (sessionId) {{
    openAuthPopup();
  }} else {{
    btn.textContent = 'Login with Microsoft';
    btn.disabled = false;
  }}
}}

function openAuthPopup() {{
  document.getElementById('auth-overlay').classList.add('open');
  document.getElementById('auth-code-input').value = '';
  document.getElementById('auth-popup-msg').textContent = '';
  document.getElementById('auth-code-input').focus();
}}

function closeAuthPopup() {{
  document.getElementById('auth-overlay').classList.remove('open');
  const btn = document.getElementById('login-btn');
  btn.textContent = 'Login with Microsoft';
  btn.disabled = false;
}}

async function submitAuthCode() {{
  const code = document.getElementById('auth-code-input').value.trim();
  if (!code) return;
  const submitBtn = document.querySelector('#auth-popup .settings-btn:last-child');
  submitBtn.disabled = true;
  submitBtn.textContent = '…';
  const msgEl = document.getElementById('auth-popup-msg');
  msgEl.textContent = 'Exchanging…';

  const result = await pyapi('exchange_auth_code', code);
  if (result.ok) {{
    msgEl.className = 'ok';
    msgEl.textContent = 'Logged in as ' + result.username;
    onLoginResult(result.username);
    setTimeout(closeAuthPopup, 1500);
  }} else {{
    msgEl.className = 'err';
    msgEl.textContent = result.error || 'Login failed.';
    submitBtn.disabled = false;
    submitBtn.textContent = 'Verify';
  }}
}}

function onLoginResult(username) {{
  const btn = document.getElementById('login-btn');
  btn.textContent = '✓ Logged in';
  btn.classList.add('verified');
  btn.disabled = true;
  document.getElementById('login-status').textContent = username;
}}

function doVerifyRepair() {{ showTab('play'); pyapi('start_repair'); }}

function saveSettings() {{
  const memMb = parseInt(document.getElementById('mem-slider').value);
  pyapi('save_settings', memMb);
  toast('Settings saved.');
}}

// ── Java dropdown ────────────────────────────────────────────────────────────
let javaOptions = [];

function populateJavaDropdown(options) {{
  javaOptions = options;
  const sel = document.getElementById('java-select');
  sel.innerHTML = '';
  options.forEach(opt => {{
    const el = document.createElement('option');
    el.value = opt.path === null ? '__auto__' : opt.path;
    el.textContent = opt.label;
    sel.appendChild(el);
  }});
  // "Custom…" entry at the end
  const customOpt = document.createElement('option');
  customOpt.value = '__custom__';
  customOpt.textContent = 'Custom…';
  sel.appendChild(customOpt);

  // Pre-select the saved path
  const currentPath = (window._savedJavaPath || '');
  if (currentPath && javaOptions.some(o => o.path === currentPath)) {{
    sel.value = currentPath;
  }} else if (currentPath) {{
    sel.value = '__custom__';
    showCustomJavaInput(currentPath);
  }} else {{
    sel.value = '__auto__';
  }}
  onJavaSelectChange();
}}

function onJavaSelectChange() {{
  const sel = document.getElementById('java-select');
  const customRow = document.getElementById('custom-java-row');
  if (sel.value === '__custom__') {{
    customRow.style.display = 'flex';
    document.getElementById('custom-java-input').value = '';
  }} else {{
    customRow.style.display = 'none';
    const path = sel.value === '__auto__' ? null : sel.value;
    pyapi('set_java_path', path);
  }}
}}

function setCustomJava() {{
  const path = document.getElementById('custom-java-input').value.trim();
  if (path) {{
    pyapi('set_java_path', path);
    toast('Custom Java path saved.');
  }}
}}

function showCustomJavaInput(path) {{
  document.getElementById('custom-java-row').style.display = 'flex';
  document.getElementById('custom-java-input').value = path;
}}

document.addEventListener('keydown', e => {{
  if (e.key === 'Escape' && document.getElementById('auth-overlay').classList.contains('open')) {{
    closeAuthPopup();
  }}
  if (e.key === 'Enter' && document.getElementById('auth-overlay').classList.contains('open')) {{
    submitAuthCode();
  }}
}});

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2800);
}}

// ── Init ─────────────────────────────────────────────────────────────────────
function initUI(cfg) {{
  const totalMb   = cfg.total_ram_mb  || 8192;
  const currentMb = cfg.jvm_memory_mb || 2048;
  const slider = document.getElementById('mem-slider');
  slider.max = totalMb; slider.value = currentMb;
  document.getElementById('ram-hint').textContent =
    'Total RAM: ' + (totalMb/1024).toFixed(1) + ' GB';
  updateMemLabel();
  if (cfg.installed_ver)
    document.getElementById('installed-ver').textContent = 'Installed: ' + cfg.installed_ver;
  if (cfg.logged_in) {{
    onLoginResult(cfg.active_account_name);
  }}

  // Store saved Java path (might be overridden by dropdown selection)
  window._savedJavaPath = cfg.java_path || '';

  // Fetch available Java installations and build the dropdown
  if (window.pywebview && window.pywebview.api) {{
    window.pywebview.api.get_java_list().then(opts => {{
      populateJavaDropdown(opts);
    }});
  }}

  const img = document.getElementById('logo-img');
  if (img && img.src) {{
    img.style.display = 'block';
    const text = document.getElementById('logo-text');
    if (text) text.style.display = 'none';
    if (img.complete) renderLogo();
    else img.onload = renderLogo;
  }}
}}
</script>
</body>
</html>"""

class API:
    def __init__(self, window_ref_holder: list, cfg: dict, game_dir: str, remote_config_url: str):
        self._wh        = window_ref_holder
        self._cfg       = cfg
        self._game_dir  = game_dir
        self._remote_url = remote_config_url
        self._dl: Installer | None = None
        self._btn_state = "idle"
        self._process: subprocess.Popen | None = None

    @property
    def _win(self):
        return self._wh[0] if self._wh else None

    def _js(self, code: str):
        if self._win:
            self._win.evaluate_js(code)

    def start_auth(self) -> str:
        """Opens the verify flow on the Flask server."""
        import secrets
        self._pending_session_id = secrets.token_hex(16)
        webbrowser.open(f"http://private.playdwp.net/verify?session={self._pending_session_id}")
        return "active"

    def exchange_auth_code(self, code: str) -> dict:
        """Directly exchange the aristois code for a profile."""
        from core.auth import _exchange_code_for_profile
        try:
            session_id = getattr(self, "_pending_session_id", None)
            if not session_id:
                return {"ok": False, "error": "No active session. Please restart login."}
            profile = _exchange_code_for_profile(code, session_id, "private.playdwp.net")
            if profile:
                self._cfg["active_account"] = {
                    "username": profile["username"],
                    "uuid":     profile["uuid"],
                    "access_token": profile["access_token"],
                }
                self._cfg["active_account_name"] = profile["username"]
                config.save(self._cfg)
                self._pending_session_id = None  # clear after use
                return {"ok": True, "username": profile["username"]}
            else:
                return {"ok": False, "error": "Code exchange failed."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Window controls
    def minimize(self):
        if self._win: self._win.minimize()

    def close(self):
        if self._win: self._win.destroy()

    def get_window_pos(self) -> dict:
        """Return the window's current screen position for the drag calculation."""
        if self._win:
            return {"x": self._win.x, "y": self._win.y}
        return {"x": 0, "y": 0}

    def move_window_abs(self, x: int, y: int):
        """Move the window to an absolute screen position."""
        if self._win:
            self._win.move(int(x), int(y))

    def open_url(self, url: str):
        webbrowser.open(url)

    def open_folder(self, subfolder: str):
        open_folder(self._cfg["game_dir"], subfolder)

    # Mods
    def get_mods(self):
        """Return list of mods from the mods folder."""
        try:
            mods_list = list_mods(self._cfg["game_dir"])
            result = []
            for m in mods_list:
                icon_b64 = ""
                if m.icon_data:
                    icon_b64 = base64.b64encode(m.icon_data).decode()
                result.append({
                    "filename": m.filename, "enabled": m.enabled,
                    "name": m.name, "version": m.version, "icon_b64": icon_b64,
                })
            return result
        except Exception as e:
            print(f"[MODS] Error loading mods: {e}")
            return []

    def toggle_mod(self, filename: str):
        try:
            toggle_mod(self._cfg["game_dir"], filename)
        except Exception as e:
            self._js(f"toast({json.dumps(str(e))})")

    # Login
    def login(self):
        def run():
            try:
                result = login_microsoft()
                if result:
                    self._cfg["active_account"] = {
                        "username": result["username"],
                        "uuid": result["uuid"],
                        "access_token": result["access_token"],
                    }
                    self._cfg["active_account_name"] = result["username"]
                    config.save(self._cfg)
                    self._js(f"onLoginResult({json.dumps(result['username'])})")
                else:
                    self._js("toast('Login failed or cancelled.')")
                    self._js("document.getElementById('login-btn').textContent = 'Login with Microsoft';")
                    self._js("document.getElementById('login-btn').disabled = false;")
            except Exception as e:
                self._js(f"toast({json.dumps(str(e))})")
                self._js("document.getElementById('login-btn').textContent = 'Login with Microsoft';")
                self._js("document.getElementById('login-btn').disabled = false;")
        threading.Thread(target=run, daemon=True).start()

    # Settings
    def save_settings(self, mem_mb: int):
        self._cfg["jvm_memory_mb"] = int(mem_mb)
        config.save(self._cfg)

    # Install / repair
    def start_download(self, repair_only: bool = False):
        self._btn_state = "downloading"
        self._js("setBtnState('downloading','DOWNLOADING','',0)")

        self._dl = Installer(
            game_dir=self._game_dir,
            remote_config_url=self._remote_url,
            on_progress=self._on_progress,
            on_status=self._on_status,
            on_done=self._on_done,
            on_error=self._on_error,
        )
        threading.Thread(target=self._dl.run, daemon=True).start()

    def start_repair(self):
        self.start_download(repair_only=True)

    def _on_progress(self, done, total, bytes_done, bytes_total):
        pct = int((done / total * 100) if total else 0)
        if self._btn_state == "downloading":
            self._js(f"setProgress({pct},'DOWNLOADING','{done}/{total} files')")
        elif self._btn_state == "verifying":
            self._js(f"setProgress({pct},'VERIFYING','{done}/{total} files')")

    def _on_status(self, msg: str):
        """Route status messages (including per-file download names) to the UI."""
        self._js(f"setStatus({json.dumps(msg)})")

    def _on_done(self):
        self._btn_state = "ready"
        self._js("setBtnState('ready','PLAY','',100)")
        self._js("setStatus('Ready to play!')")
        ver = version.local_version(self._game_dir)
        if ver:
            self._js(f"setVersion('Version: {ver.get('version', '?')}')")

    def _on_error(self, msg: str):
        self._btn_state = "error"
        self._js(f"toast({json.dumps(msg)})")
        self._js("setBtnState('error','RETRY','',0)")

    def launch_game(self):
        """Launch the Minecraft game."""
        def run():
            try:
                account = self._cfg.get("active_account")
                if not account:
                    self._js("toast('No account logged in')")
                    self._js("setBtnState('idle','PLAY','',0)")
                    return
                
                print(f"[LAUNCH] Starting game for {account['username']}")
                print(f"[LAUNCH] Game dir: {self._game_dir}")
                
                ver_info = version.local_version(self._game_dir)
                mc_ver = ver_info.get("mc_version", "1.20") if ver_info else "1.20"
                
                custom_java = self._cfg.get("java_path")          # <-- NEW
                
                err, proc = launch(
                    game_dir=self._game_dir,
                    mc_ver=mc_ver,
                    jvm_mb=self._cfg.get("jvm_memory_mb", 2048),
                    username=account["username"],
                    access_token=account["access_token"],
                    uuid=account["uuid"],
                    custom_java=custom_java,                     # <-- NEW
                )
                
                if err:
                    print(f"[LAUNCH] Error: {err}")
                    self._js(f"toast({json.dumps(err)})")
                    self._js("setBtnState('error','RETRY','',0)")
                    return
                
                self._process = proc
                print(f"[LAUNCH] Game launched with PID {proc.pid}")
                self._js("setBtnState('running','STOP','running',100)")
                self._watch_process()
            except Exception as e:
                print(f"[LAUNCH] Exception: {type(e).__name__}: {str(e)}")
                self._js(f"toast({json.dumps(str(e))})")
                self._js("setBtnState('error','RETRY','',0)")
        
        threading.Thread(target=run, daemon=True).start()

    def _watch_process(self):
        """Watch the game process and update UI when it closes."""
        if not self._process:
            return
        print(f"[LAUNCH] Watching process {self._process.pid}")
        try:
            self._process.wait(timeout=300)
            print(f"[LAUNCH] Game exited with code {self._process.returncode}")
        except Exception as e:
            print(f"[LAUNCH] Error watching process: {e}")
        finally:
            self._js("setBtnState('ready','PLAY','',100)")
            self._js("setStatus('Game closed')")
            self._process = None

    def stop_game(self):
        """Stop the running game process."""
        if self._process:
            try:
                print(f"[LAUNCH] Terminating process {self._process.pid}")
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception as e:
                print(f"[LAUNCH] Error stopping process: {e}")
                try:
                    self._process.kill()
                except:
                    pass
            finally:
                self._process = None
                self._js("setBtnState('ready','PLAY','',100)")

    def check_version(self):
        def work():
            needs, local, remote = version.needs_update(self._game_dir, self._remote_url)
            if local == "none":
                label, state, lbl = f"Not installed  |  Server: {remote}", "idle", "DOWNLOAD"
            elif needs:
                label, state, lbl = f"Update available  {local} → {remote}", "idle", "UPDATE"
            else:
                label, state, lbl = f"Version: {local}", "ready", "PLAY"
            self._btn_state = state
            self._js(f"setVersion({json.dumps(label)})")
            if state == "ready":
                self._js("setBtnState('ready','PLAY','',100)")
            else:
                self._js(f"setBtnState('idle',{json.dumps(lbl)},'',0)")
            if needs and local != "none":
                self.start_download()
        threading.Thread(target=work, daemon=True).start()


    def get_java_list(self) -> list[dict]:
        """Return all discovered Java installations for the dropdown."""
        from core.game_launcher import find_all_java_installations
        paths = find_all_java_installations(self._cfg["game_dir"])
        options = [{"label": "Auto-detect", "path": None}]
        for p in paths:
            options.append({"label": str(p), "path": str(p)})
        # If a custom path is set and it's not already in the list, add it
        custom = self._cfg.get("java_path")
        if custom and not any(o["path"] == custom for o in options):
            options.append({"label": f"Custom: {custom}", "path": custom})
        return options

    def set_java_path(self, path: str | None):
        """Save the selected Java path (None for auto)."""
        if path == "__auto__" or path is None or path == "":
            self._cfg["java_path"] = None
        else:
            self._cfg["java_path"] = path
        config.save(self._cfg)
        self._js("toast('Java path updated.')")


def run(remote_config_url: str, game_dir: str):
    cfg = config.load()
    cfg["game_dir"] = config.resolve_game_dir(game_dir)
    config.save(cfg)

    logo_data = _find_logo(cfg["game_dir"])
    html = _build_html(logo_data)

    window_holder: list = []
    api = API(window_holder, cfg, cfg["game_dir"], remote_config_url)

    def on_loaded():
        ver = version.local_version(cfg["game_dir"])
        ver_str = ver.get("version", "Not installed") if ver else "Not installed"
        init_data = {
            "jvm_memory_mb":       cfg.get("jvm_memory_mb") or config.default_jvm_mb(),
            "total_ram_mb":        config.get_total_ram_mb(),
            "active_account_name": cfg.get("active_account_name", ""),
            "installed_ver":       ver_str,
            "logged_in":           bool(cfg.get("active_account")),
            "java_path":           cfg.get("java_path") or "",
        }
        window_holder[0].evaluate_js(f"initUI({json.dumps(init_data)})")
        api.check_version()

    win = webview.create_window(
        title            = "DWP Launcher",
        html             = html,
        js_api           = api,
        width            = 720,
        height           = 480,
        resizable        = True,
        frameless        = True,
        easy_drag        = False,   # keep off — we handle drag ourselves
        background_color = "#0a0a0a",
    )
    window_holder.append(win)
    win.events.loaded += on_loaded
    webview.start(debug=False)
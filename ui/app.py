"""
ui/app.py  –  DWP Launcher using pywebview.

Verify flow (all in system browser, no embedded webview navigation):
  1. start_verify() generates a random session_id
  2. Opens https://<domain>/verify?session=<id>  in the system browser
     → user does Discord OAuth in browser
     → browser shows "return to launcher" page
  3. Simultaneously opens https://auth.aristois.net/auth  in a second browser tab
  4. Shows the in-app code-entry popup
  5. User pastes aristois code into popup → launcher POSTs to /verify/submit
  6. on_verify_complete() fires on success
"""

import base64
import json
import os
import threading
import sys
import webbrowser
import time
import secrets
from pathlib import Path

import requests
import webview

from core import config, version
from core.downloader import Downloader
from core.game_launcher import launch, find_java
from core.mods import list_mods, toggle_mod, open_folder


def _find_logo(game_dir: str) -> str:
    search_names = [
        "logo.png", "logo.jpg", "logo.gif", "logo.webp",
        "icon.png", "icon.jpg", "dwp.png", "dwp.jpg",
        "launcher_logo.png", "launcher_icon.png",
    ]
    search_dirs = [
        Path(game_dir),
        Path(game_dir).parent,
        Path(sys.argv[0]).parent if sys.argv[0] else Path("."),
        Path("."),
        Path(__file__).parent.parent,
    ]
    for d in search_dirs:
        for name in search_names:
            candidate = d / name
            if candidate.exists():
                try:
                    ext  = candidate.suffix.lower().lstrip(".")
                    mime = {"png": "image/png", "jpg": "image/jpeg",
                            "jpeg": "image/jpeg", "gif": "image/gif",
                            "webp": "image/webp"}.get(ext, "image/png")
                    data = base64.b64encode(candidate.read_bytes()).decode()
                    return f"data:{mime};base64,{data}"
                except Exception:
                    pass
    return ""


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

  /* ── Drag strip (top 28px — contains only window controls, nothing else) ── */
  #drag-strip {{
    flex: 0 0 28px;
    position: relative;
    -webkit-app-region: drag;
    app-region: drag;
    background: transparent;
    z-index: 200;
  }}
  #win-controls {{
    position: absolute; right: 10px; top: 50%;
    transform: translateY(-50%);
    display: flex; gap: 4px;
    -webkit-app-region: no-drag;
    app-region: no-drag;
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

  /* ── Content ─────────────────────────────────────────────── */
  #content {{ flex: 1; position: relative; overflow: hidden; }}

  /* ── Pages ───────────────────────────────────────────────── */
  .page {{
    position: absolute; inset: 0;
    display: none;
    height: 100%; width: 100%;
  }}
  .page.active {{ display: flex; flex-direction: column; }}

  /* ── PLAY ────────────────────────────────────────────────── */
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

  /* Logo: ring is a ::before pseudo, image canvas sits above it */
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
  #logo-img {{ display: none; }}

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

  /* ── MODS ────────────────────────────────────────────────── */
  #page-mods {{ background: var(--bg); position: relative; min-height: 100%;}}
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
    height: 100%;
  }}
  #mods-header {{ display: flex; align-items: center; justify-content: space-between; }}
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
    flex: 1; background: rgba(20,20,20,0.85);
    border: 1px solid var(--border); border-radius: 10px;
    overflow-y: auto; padding: 14px; min-height: 0;
  }}
  #mods-grid-wrap::-webkit-scrollbar {{ width: 5px; }}
  #mods-grid-wrap::-webkit-scrollbar-track {{ background: transparent; }}
  #mods-grid-wrap::-webkit-scrollbar-thumb {{ background: var(--bg4); border-radius: 3px; }}
  #mods-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px;
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

  /* ── SETTINGS ────────────────────────────────────────────── */
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
  #verify-status {{ font-size: 10px; color: var(--text3); padding: 0 4px; }}

  /* ── Floating bubble dock ────────────────────────────────── */
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

  /* ── Verify code popup overlay ───────────────────────────── */
  #verify-overlay {{
    display: none;
    position: fixed; inset: 0; z-index: 500;
    background: rgba(0,0,0,0.7);
    align-items: center; justify-content: center;
    backdrop-filter: blur(4px);
  }}
  #verify-overlay.open {{ display: flex; }}
  #verify-popup {{
    width: 380px;
    background: #111; border: 1px solid #2a2a2a; border-radius: 14px;
    padding: 28px 26px; display: flex; flex-direction: column; gap: 16px;
  }}
  #verify-popup h3 {{
    font-family: 'Rajdhani', sans-serif; font-size: 18px; font-weight: 700;
    letter-spacing: 0.5px; color: var(--text);
  }}
  .popup-steps {{ display: flex; flex-direction: column; gap: 10px; }}
  .popup-step {{
    display: flex; gap: 10px; align-items: flex-start; font-size: 11px; color: #888;
    line-height: 1.5;
  }}
  .step-dot {{
    width: 18px; height: 18px; border-radius: 50%;
    background: #1a1a1a; border: 1px solid #333;
    display: flex; align-items: center; justify-content: center;
    font-size: 9px; font-weight: 700; color: #666; flex-shrink: 0; margin-top: 1px;
  }}
  .step-dot.done {{ background: #2d8a3a22; border-color: var(--green); color: var(--green); }}
  #popup-code-row {{ display: flex; gap: 8px; }}
  #popup-code-input {{
    flex: 1; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 7px;
    color: var(--text); padding: 8px 12px;
    font-family: 'Inter', sans-serif; font-size: 13px; outline: none;
    transition: border-color 0.15s;
  }}
  #popup-code-input:focus {{ border-color: #555; }}
  #popup-code-input::placeholder {{ color: #444; }}
  #popup-submit {{
    padding: 8px 16px; background: var(--crimson); border: 1px solid var(--crimson2);
    border-radius: 7px; color: #fff;
    font-family: 'Inter', sans-serif; font-size: 12px; font-weight: 600;
    cursor: pointer; transition: background 0.15s; white-space: nowrap;
  }}
  #popup-submit:hover:not(:disabled) {{ background: var(--crimson2); }}
  #popup-submit:disabled {{ opacity: 0.5; cursor: default; }}
  #popup-cancel {{
    background: none; border: none; color: var(--text3);
    font-size: 11px; cursor: pointer; align-self: center;
    padding: 4px 8px; transition: color 0.15s;
  }}
  #popup-cancel:hover {{ color: var(--text2); }}
  #popup-msg {{ font-size: 11px; min-height: 14px; }}
  #popup-msg.err {{ color: #c0293f; }}
  #popup-msg.ok  {{ color: var(--green); }}

  /* ── Toast ───────────────────────────────────────────────── */
  #toast {{
    position: fixed; bottom: 84px; left: 50%;
    transform: translateX(-50%) translateY(10px);
    background: var(--bg3); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 20px; font-size: 12px; color: var(--text2);
    opacity: 0; pointer-events: none; transition: opacity 0.2s, transform 0.2s; z-index: 999;
  }}
  #toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
</style>
</head>
<body>

<!-- Drag strip + window controls -->
<div id="drag-strip">
  <div id="win-controls">
    <div class="win-btn" onclick="pyapi('minimize')">–</div>
    <div class="win-btn" id="btn-close" onclick="pyapi('close')">✕</div>
  </div>
</div>

<!-- All pages -->
<div id="content">

  <!-- PLAY -->
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

  <!-- MODS -->
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

  <!-- SETTINGS -->
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
        <div class="settings-section-title">Account</div>
        <div class="settings-row">
          <div class="settings-label">Verify Account</div>
          <div class="settings-control">
            <button class="settings-btn secondary" id="verify-btn" onclick="doVerify()">Verify Ownership</button>
            <span id="verify-status"></span>
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
        <div class="settings-section-title">Server & URLs</div>
        <div class="settings-row">
          <div class="settings-label">Server URL</div>
          <div class="settings-control">
            <input class="settings-input" type="text" id="server-url-input">
          </div>
        </div>
        <div class="settings-row">
          <div class="settings-label">Download URL</div>
          <div class="settings-control">
            <input class="settings-input" type="text" id="download-url-input">
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

  <!-- Floating dock -->
  <div id="dock">
    <button class="dock-btn active" data-tab="play"     onclick="showTab('play')">Play</button>
    <div class="dock-sep"></div>
    <button class="dock-btn"        data-tab="mods"     onclick="showTab('mods')">Mods</button>
    <div class="dock-sep"></div>
    <button class="dock-btn"        data-tab="settings" onclick="showTab('settings')">Settings</button>
  </div>

</div><!-- #content -->

<!-- Verify code popup -->
<div id="verify-overlay">
  <div id="verify-popup">
    <h3>Verify Minecraft Account</h3>
    <div class="popup-steps">
      <div class="popup-step">
        <div class="step-dot done" id="step1-dot">✓</div>
        <div>Your browser opened the Discord login — complete it there, then return here.</div>
      </div>
      <div class="popup-step">
        <div class="step-dot" id="step2-dot">2</div>
        <div>A second browser tab opened <strong>auth.aristois.net</strong>. Sign in with the Microsoft account that owns your Minecraft profile and copy the code shown.</div>
      </div>
      <div class="popup-step">
        <div class="step-dot" id="step3-dot">3</div>
        <div>Paste the aristois code below:</div>
      </div>
    </div>
    <div id="popup-code-row">
      <input id="popup-code-input" type="text" placeholder="Paste aristois code…"
             autocomplete="off" spellcheck="false">
      <button id="popup-submit" onclick="submitVerifyCode()">Verify</button>
    </div>
    <div id="popup-msg"></div>
    <button id="popup-cancel" onclick="closeVerifyPopup()">Cancel</button>
  </div>
</div>

<div id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let btnState      = 'idle';
let verifySession = null;   // current session_id during verify flow

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(tab) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.dock-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + tab).classList.add('active');
  document.querySelector(`.dock-btn[data-tab="${{tab}}"]`).classList.add('active');
  if (tab === 'mods') refreshMods();
}}

// ── Python bridge ─────────────────────────────────────────────────────────────
function pyapi(action, ...args) {{
  if (window.pywebview && window.pywebview.api) {{
    return window.pywebview.api[action](...args);
  }}
}}

// ── Play ──────────────────────────────────────────────────────────────────────
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
  if (!img || !img.src || img.src === window.location.href) return;
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
  grid.innerHTML = mods.map(mod => `
    <div class="mod-card">
      <div class="mod-icon">
        ${{mod.icon_b64 ? `<img src="data:image/png;base64,${{mod.icon_b64}}" alt="">` : '🎮'}}
      </div>
      <div class="mod-info">
        <div class="mod-name">${{mod.name}}</div>
        <div class="mod-version">${{mod.version || ''}}</div>
      </div>
      <button class="mod-toggle ${{mod.enabled ? 'on' : 'off'}}"
              onclick="toggleMod('${{mod.filename}}')"></button>
    </div>
  `).join('');
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

function doVerify() {{
  const btn = document.getElementById('verify-btn');
  btn.textContent = 'Opening…';
  btn.disabled = true;
  const r = pyapi('start_verify');
  // Popup will open once Python confirms browsers opened
  if (r && r.then) r.then(sessionId => {{
    if (sessionId) openVerifyPopup(sessionId);
    else {{ btn.textContent = 'Verify Ownership'; btn.disabled = false; }}
  }});
}}

// ── Verify popup ──────────────────────────────────────────────────────────────
function openVerifyPopup(sessionId) {{
  verifySession = sessionId;
  document.getElementById('verify-overlay').classList.add('open');
  document.getElementById('popup-code-input').value = '';
  document.getElementById('popup-msg').textContent = '';
  document.getElementById('popup-msg').className = '';
  document.getElementById('popup-submit').disabled = false;
  document.getElementById('popup-submit').textContent = 'Verify';
  document.getElementById('popup-code-input').disabled = false;
  setTimeout(() => document.getElementById('popup-code-input').focus(), 100);
}}

function closeVerifyPopup() {{
  document.getElementById('verify-overlay').classList.remove('open');
  verifySession = null;
  const btn = document.getElementById('verify-btn');
  btn.textContent = 'Verify Ownership';
  btn.disabled = false;
}}

async function submitVerifyCode() {{
  const code = document.getElementById('popup-code-input').value.trim();
  if (!code || !verifySession) return;

  const submitBtn = document.getElementById('popup-submit');
  const msg       = document.getElementById('popup-msg');
  submitBtn.disabled = true;
  submitBtn.textContent = '…';
  msg.className = '';
  msg.textContent = 'Verifying…';

  const r = pyapi('submit_verify_code', verifySession, code);
  if (r && r.then) {{
    r.then(result => {{
      if (result && result.ok) {{
        msg.className = 'ok';
        msg.textContent = '✓ Verified as ' + result.username + '!';
        submitBtn.textContent = 'Done';
        document.getElementById('popup-code-input').disabled = true;
        setTimeout(closeVerifyPopup, 1800);
        onVerifyComplete(result.username);
      }} else {{
        msg.className = 'err';
        msg.textContent = (result && result.error) || 'Verification failed.';
        submitBtn.disabled = false;
        submitBtn.textContent = 'Retry';
      }}
    }});
  }}
}}

document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') closeVerifyPopup();
  if (e.key === 'Enter' &&
      document.getElementById('verify-overlay').classList.contains('open')) {{
    submitVerifyCode();
  }}
}});

function onVerifyComplete(username) {{
  const btn = document.getElementById('verify-btn');
  btn.textContent = '✓ Verified';
  btn.classList.add('verified');
  btn.disabled = true;
  document.getElementById('verify-status').textContent = username;
  toast('Account verified as ' + username + '!');
}}

// ── Repair ────────────────────────────────────────────────────────────────────
function doVerifyRepair() {{ showTab('play'); pyapi('start_repair'); }}

// ── Save settings ─────────────────────────────────────────────────────────────
function saveSettings() {{
  const serverUrl = document.getElementById('server-url-input').value.trim();
  const downloadUrl = document.getElementById('download-url-input').value.trim();
  const memMb     = parseInt(document.getElementById('mem-slider').value);
  const r = pyapi('save_settings', serverUrl, downloadUrl, memMb);
  if (r && r.then) r.then(() => toast('Settings saved.'));
  else toast('Settings saved.');
}}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2800);
}}

// ── Init ──────────────────────────────────────────────────────────────────────
function initUI(cfg) {{
  const totalMb   = cfg.total_ram_mb  || 8192;
  const currentMb = cfg.jvm_memory_mb || 2048;
  const slider = document.getElementById('mem-slider');
  slider.max = totalMb; slider.value = currentMb;
  document.getElementById('ram-hint').textContent =
    `Total RAM: ${{(totalMb/1024).toFixed(1)}} GB`;
  updateMemLabel();
  document.getElementById('server-url-input').value = cfg.server_url || '';
  document.getElementById('download-url-input').value = cfg.download_url || '';
  if (cfg.installed_ver)
    document.getElementById('installed-ver').textContent = 'Installed: ' + cfg.installed_ver;
  if (cfg.verified) {{
    const btn = document.getElementById('verify-btn');
    btn.textContent = '✓ Verified';
    btn.classList.add('verified');
    btn.disabled = true;
    if (cfg.active_account_name)
      document.getElementById('verify-status').textContent = cfg.active_account_name;
  }}
  const img = document.getElementById('logo-img');
  if (img) {{
    if (img.complete) renderLogo();
    else img.onload = renderLogo;
  }}
}}
</script>
</body>
</html>"""


class API:
    def __init__(self, window_ref_holder: list, cfg: dict, game_dir: str):
        self._wh        = window_ref_holder
        self._cfg       = cfg
        self._game_dir  = game_dir
        self._dl: Downloader | None = None
        self._btn_state = "idle"

        self._process: subprocess.Popen | None = None

    @property
    def _win(self):
        return self._wh[0] if self._wh else None

    def _js(self, code: str):
        if self._win:
            self._win.evaluate_js(code)

    def _server_base(self) -> str:
        url = self._cfg.get("server_url", "")
        # Normalise to just scheme + host, e.g. https://playdwp.net
        url = url.rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        # Strip any path after the host
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    # ── Window ─────────────────────────────────────────────────────────────
    def minimize(self):
        if self._win: self._win.minimize()

    def close(self):
        if self._win: self._win.destroy()

    def open_url(self, url: str):
        webbrowser.open(url)

    def open_folder(self, subfolder: str):
        open_folder(self._cfg["game_dir"], subfolder)

    # ── Mods ───────────────────────────────────────────────────────────────
    def get_mods(self):
        mods = list_mods(self._cfg["game_dir"])
        result = []
        for m in mods:
            icon_b64 = ""
            if m.icon_data:
                icon_b64 = base64.b64encode(m.icon_data).decode()
            result.append({
                "filename": m.filename, "enabled": m.enabled,
                "name": m.name, "version": m.version, "icon_b64": icon_b64,
            })
        return result

    def toggle_mod(self, filename: str):
        try:
            toggle_mod(self._cfg["game_dir"], filename)
        except Exception as e:
            self._js(f"toast({json.dumps(str(e))})")

    # ── Verify ─────────────────────────────────────────────────────────────
    def start_verify(self) -> str | None:
        """
        Called from JS when the user clicks Verify Ownership.
        1. Generates a session_id
        2. Opens /verify?session=<id> in the system browser (Discord OAuth)
        3. Opens https://auth.aristois.net/auth in a second browser tab
        4. Returns the session_id so JS can open the popup
        """
        base = self._server_base()
        if not base or base == "https://":
            self._js("toast('Server URL not configured — cannot verify.')")
            return None

        session_id = secrets.token_hex(16)
        verify_url = f"{base}/verify?session={session_id}"

        webbrowser.open(verify_url)

        return session_id   # JS receives this and opens the popup

    def submit_verify_code(self, session_id: str, aristois_code: str) -> dict:
        """
        Called from JS when the user submits their aristois code in the popup.
        POSTs to /verify/submit on the Flask server.
        Returns { ok, username, uuid } or { error }.
        """
        base = self._server_base()
        try:
            r = requests.post(
                f"{base}/verify/submit",
                json={"session_id": session_id, "aristois_code": aristois_code},
                timeout=20,
            )
            data = r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        if data.get("custom_token"):
            self._cfg["mc_custom_token"]     = data["custom_token"]
            self._cfg["active_account_name"] = data["username"]
            self._cfg["active_account_uuid"] = data["uuid"]
            config.save(self._cfg)
            return {"ok": True, "username": data["username"], "uuid": data["uuid"]}

        return {"ok": False, "error": data.get("error", "Unknown error")}

    # ── Settings ───────────────────────────────────────────────────────────
    def save_settings(self, server_url: str, download_url: str, mem_mb: int):
        self._cfg["server_url"]    = server_url
        self._cfg["download_url"]    = download_url
        self._cfg["jvm_memory_mb"] = int(mem_mb)
        config.save(self._cfg)

    # ── Download / Repair ──────────────────────────────────────────────────
    def start_download(self, repair_only: bool = False):
        self._repair_active = repair_only
        if self._btn_state in ("downloading", "verifying"):
            return
        if not repair_only and version.mc_version_changed(
                self._cfg["game_dir"], self._cfg["download_url"]):
            import shutil
            mods_path = Path(self._cfg["game_dir"]) / "mods"
            if mods_path.exists():
                shutil.rmtree(mods_path)
                self._js("setStatus('Removed outdated mods due to MC version change.')")

        self._btn_state = "downloading"
        self._js("setBtnState('downloading','DOWNLOADING','',0)")

        self._dl = Downloader(
            server_url  = self._cfg["server_url"],
            download_url= self._cfg["download_url"],
            game_dir    = self._cfg["game_dir"],
            on_progress = self._on_progress,
            on_status   = self._on_status,
            on_phase    = self._on_phase,
            on_done     = self._on_done,
            on_error    = self._on_error,
            repair_only = repair_only,
        )
        threading.Thread(target=self._dl.run, daemon=True).start()

    def start_repair(self):
        self.start_download(repair_only=True)

    def _on_progress(self, done, total, bytes_done, bytes_total):
        pct = int((done / total * 100) if total else 0)
        if self._btn_state == "verifying":
            # Verify/repair passes file counts in both pairs, not bytes
            self._js(f"setProgress({pct},'{"REPAIRING" if getattr(self,"_repair_active",False) else "VERIFYING"}','{done}/{total} files')")
        elif self._btn_state == "downloading":
            mb   = bytes_done  / 1_048_576
            mb_t = bytes_total / 1_048_576
            self._js(f"setProgress({pct},'DOWNLOADING','{pct}%  {mb:.0f}/{mb_t:.0f} MB')")

    def _on_status(self, msg: str):
        self._js(f"setStatus({json.dumps(msg)})")

    def _on_phase(self, phase: str):
        if phase == "downloading":
            self._btn_state = "downloading"
            self._js("setBtnState('downloading','DOWNLOADING','',0)")
        elif phase == "verifying":
            self._btn_state = "verifying"
            self._js("setBtnState('verifying','VERIFYING','',0)")
        elif phase == "done":
            self._btn_state = "ready"
            self._js("setBtnState('ready','PLAY','',100)")
            self._js("setStatus('Ready to play!')")
            ver = version.local_version(self._cfg["game_dir"])
            if ver:
                self._js(f"setVersion('Version: {ver.get('version', '?')}')")
        elif phase == "verifying":
          self._btn_state = "verifying"
          label = "REPAIRING" if getattr(self, '_repair_active', False) else "VERIFYING"
          self._js(f"setBtnState('verifying',{json.dumps(label)},'',0)")
                
        elif phase == "error":
            self._btn_state = "error"
            self._js("setBtnState('error','RETRY','',0)")

    def _on_done(self): pass

    def _on_error(self, msg: str):
        self._js(f"toast({json.dumps(msg)})")

    # ── Launch ─────────────────────────────────────────────────────────────
    def launch_game(self):
        ver    = version.local_version(self._cfg["game_dir"])
        mc_ver = ver.get("mc_version") if ver else None

        import re
        dir_ver = Path(self._cfg["game_dir"]).name
        if re.fullmatch(r"\d+(?:\.\d+)*", dir_ver):
            mc_ver = dir_ver
        if not mc_ver:
            mc_ver = "26.1.2"

        jvm_mb       = self._cfg.get("jvm_memory_mb") or config.default_jvm_mb()
        account      = self._cfg.get("active_account")
        custom_token = self._cfg.get("mc_custom_token")

        if not account or isinstance(account, str):
            if custom_token:
                account = {
                    "username":     self._cfg.get("active_account_name", "Player"),
                    "uuid":         self._cfg.get("active_account_uuid", "00000000-0000-0000-0000-000000000000"),
                    "access_token": custom_token,
                }
            else:
                self._js("toast('Please verify your account first (Settings tab).')")
                return

        err, proc = launch(
            game_dir     = self._cfg["game_dir"],
            mc_version   = mc_ver,
            jvm_mb       = jvm_mb,
            username     = account["username"],
            access_token = account["access_token"],
            uuid         = account["uuid"],
        )
        if err:
            self._js(f"toast({json.dumps(err)})")
            self._js("setBtnState('ready','PLAY','',100)")
            return
        self._process = proc
        self._js("setBtnState('running','STOP','Game running',100)")
        threading.Thread(target=self._watch_process, daemon=True).start()

    def _watch_process(self):
        if self._process:
            self._process.wait()          # blocks until game exits
        self._process = None
        self._btn_state = "ready"
        self._js("setBtnState('ready','PLAY','',100)")
        self._js("setStatus('Game closed.')")

    def stop_game(self):
        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
    # ── Version check ───────────────────────────────────────────────────────
    def check_version(self):
        def work():
            needs, local, remote = version.needs_update(
                self._cfg["game_dir"], self._cfg["download_url"])
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


def run(server_url: str, game_dir: str):
    cfg = config.load()
    cfg["server_url"] = server_url
    cfg["game_dir"]   = config.resolve_game_dir(game_dir)
    config.save(cfg)

    logo_data = _find_logo(cfg["game_dir"])
    html      = _build_html(logo_data)

    window_holder: list = []
    api = API(window_holder, cfg, cfg["game_dir"])

    def on_loaded():
        ver     = version.local_version(cfg["game_dir"])
        ver_str = ver.get("version", "Not installed") if ver else "Not installed"
        init_data = {
            "server_url":          cfg.get("server_url", ""),
            "download_url":        cfg.get("download_url", ""),
            "jvm_memory_mb":       cfg.get("jvm_memory_mb") or config.default_jvm_mb(),
            "total_ram_mb":        config.get_total_ram_mb(),
            "active_account_name": cfg.get("active_account_name", ""),
            "active_account_uuid": cfg.get("active_account_uuid", ""),
            "installed_ver":       ver_str,
            "verified":            bool(cfg.get("mc_custom_token")),
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
      easy_drag        = False,
        background_color = "#0a0a0a",
    )
    window_holder.append(win)
    win.events.loaded += on_loaded
    webview.start(debug=False)
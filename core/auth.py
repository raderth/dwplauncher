"""
core/auth.py – Microsoft/Xbox/Minecraft auth via auth.aristois.net
Uses the redirect-code flow that matches the webapp's /msa/ and /msa-callback
endpoints, replacing the old device-code flow entirely.

Output contract (unchanged):
{
    "username":     str,
    "uuid":         str,   # with dashes
    "access_token": str,   # raw Minecraft bearer token
}
"""

import threading
import webbrowser
import time
import requests
from urllib.parse import urlencode

# Must match the registered redirect URI in the aristois / Azure app
MSA_CLIENT_ID = "b35593c4-f505-47e4-9a45-4f0d24c3c007"
AUTH_BASE      = "https://auth.aristois.net"


def _format_uuid(raw: str) -> str:
    if len(raw) == 32:
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    return raw


def _exchange_code_for_profile(auth_code: str, redirect_uri: str):
    """
    Takes a Microsoft Live auth-code, walks the full auth chain, and
    returns the same dict the old login_microsoft() returned.
    """
    # 1. Exchange code → MS access token via aristois proxy
    r = requests.post(
        f"{AUTH_BASE}/token",
        data={
            "client_id":    MSA_CLIENT_ID,
            "code":         auth_code,
            "redirect_uri": redirect_uri,
            "grant_type":   "authorization_code",
        },
        timeout=15,
    )
    token_data = r.json()
    ms_token = token_data.get("access_token")
    if not ms_token:
        print("Token exchange failed:", token_data)
        return None

    # 2. Xbox Live
    xbl = requests.post(
        "https://user.auth.xboxlive.com/user/authenticate",
        json={
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName":  "user.auth.xboxlive.com",
                "RpsTicket": f"d={ms_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        },
        timeout=15,
    ).json()
    xbl_token = xbl.get("Token")
    uhs = xbl.get("DisplayClaims", {}).get("xui", [{}])[0].get("uhs")
    if not xbl_token or not uhs:
        print("XBL failed:", xbl)
        return None

    # 3. XSTS
    xsts = requests.post(
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        json={
            "Properties": {
                "SandboxId":  "RETAIL",
                "UserTokens": [xbl_token],
            },
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        },
        timeout=15,
    ).json()
    xsts_token = xsts.get("Token")
    if not xsts_token:
        print("XSTS failed:", xsts)
        return None

    # 4. Minecraft
    mc = requests.post(
        "https://api.minecraftservices.com/authentication/login_with_xbox",
        json={"identityToken": f"XBL3.0 x={uhs};{xsts_token}"},
        timeout=15,
    ).json()
    mc_token = mc.get("access_token")
    if not mc_token:
        print("MC auth failed:", mc)
        return None

    # 5. Profile
    profile = requests.get(
        "https://api.minecraftservices.com/minecraft/profile",
        headers={"Authorization": f"Bearer {mc_token}"},
        timeout=15,
    ).json()
    if "id" not in profile:
        print("Profile failed:", profile)
        return None

    return {
        "username":     profile["name"],
        "uuid":         _format_uuid(profile["id"]),
        "access_token": mc_token,
    }


def login_microsoft(domain: str = "localhost"):
    """
    Opens the Microsoft Live OAuth page in the system browser, starts a
    tiny local HTTP server to catch the redirect, then walks the full auth
    chain.  Returns the same dict as the old implementation, or None on failure.

    In a pywebview launcher you should call this via the API bridge and let
    the webview navigate to the Live URL directly so the callback lands in
    /msa-callback on your Flask server instead of here.  This function is
    kept as a fallback for CLI / non-webview use.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    CALLBACK_PORT = 9876
    redirect_uri  = f"https://auth.aristois.net/auth"

    result_holder: list = []
    stop_event = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # silence access logs

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404); self.end_headers(); return

            qs   = parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]

            if code:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Login complete. You may close this tab.</h2></body></html>")
                result_holder.append(code)
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"No code received.")
            stop_event.set()

    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    live_url = (
        f"https://login.live.com/oauth20_authorize.srf"
        f"?client_id={MSA_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&scope=XboxLive.signin"
    )
    print(f"Opening browser for Microsoft login…\n{live_url}")
    webbrowser.open(live_url)

    stop_event.wait(timeout=300)
    server.shutdown()

    if not result_holder:
        print("Login timed out or was cancelled.")
        return None

    return _exchange_code_for_profile(result_holder[0], redirect_uri)
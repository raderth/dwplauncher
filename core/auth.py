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
    Takes an auth code from aristois and exchanges it for a Minecraft profile.
    Uses the /token/{id} endpoint as per the API documentation.
    
    Expected response format:
    {
        'status': 'success',
        'message': 'Token has been invalidated',
        'uuid': 'ed677adf-c1e2-4b43-a28f-a680e915424e',
        'username': 'Raderth'
    }
    """
    # Exchange code via aristois token endpoint
    try:
        token_url = f"{AUTH_BASE}/token/{auth_code}"
        print(f"[AUTH] Fetching profile from {token_url}")
        r = requests.get(token_url, timeout=15)
        r.raise_for_status()
        token_data = r.json()
        print(f"[AUTH] Response status: {token_data.get('status')}")
        print(f"[AUTH] Response data: {token_data}")
    except requests.exceptions.HTTPError as e:
        print(f"[AUTH] HTTP error: {e.response.status_code} - {e.response.text}")
        raise ValueError(f"Token lookup failed: HTTP {e.response.status_code}")
    except Exception as e:
        print(f"[AUTH] Error: {type(e).__name__}: {str(e)}")
        raise ValueError(f"Token lookup failed: {str(e)}")
    
    # Check if status is success (not 'ok')
    status = token_data.get("status", "").lower()
    if status != "success":
        msg = token_data.get("message", "Token verification failed")
        print(f"[AUTH] Failed with status '{status}': {msg}")
        raise ValueError(f"Authentication failed: {msg}")
    
    # Extract profile data from response
    username = token_data.get("username")
    uuid = token_data.get("uuid")
    
    if not username or not uuid:
        print(f"[AUTH] Missing username or uuid in response: {token_data}")
        raise ValueError(f"Incomplete authentication response - missing username or uuid")
    
    # Format UUID if needed (should already have dashes but just in case)
    if len(uuid.replace("-", "")) == 32:
        uuid = _format_uuid(uuid.replace("-", ""))
    
    # Use the code/token as the access token for now
    access_token = auth_code
    
    print(f"[AUTH] Successfully authenticated: {username} ({uuid})")
    
    return {
        "username":     username,
        "uuid":         uuid,
        "access_token": access_token,
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
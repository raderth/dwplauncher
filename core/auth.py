"""
core/auth.py – Microsoft/Xbox/Minecraft auth via auth.aristois.net
Uses the redirect-code flow that matches the webapp's /msa/ and /msa-callback
endpoints, replacing the old device-code flow entirely.

Output contract (unchanged):
{
    "username":     str,
    "uuid":         str,   # with dashes
    "access_token": str,   # custom_token issued by Flask server
}
"""

import threading
import webbrowser
import time
import requests
from urllib.parse import urlencode

MSA_CLIENT_ID = "b35593c4-f505-47e4-9a45-4f0d24c3c007"
AUTH_BASE      = "https://auth.aristois.net"


def _format_uuid(raw: str) -> str:
    if len(raw) == 32:
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
    return raw


def _exchange_code_for_profile(aristois_code: str, session_id: str, server_domain: str):
    """
    Delegates the Aristois code exchange to the Flask server's /verify/submit
    endpoint. The server validates the code, checks the whitelist, and returns
    the custom_token which is used as the Minecraft access_token.

    Expected response from /verify/submit:
    {
        "custom_token": str,
        "username":     str,
        "uuid":         str,
    }
    """
    submit_url = f"http://{server_domain}/verify/submit"
    print(f"[AUTH] Submitting Aristois code to {submit_url}")

    try:
        r = requests.post(
            submit_url,
            json={
                "session_id":    session_id,
                "aristois_code": aristois_code,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        msg = body.get("error", f"HTTP {e.response.status_code}")
        print(f"[AUTH] /verify/submit error: {msg}")
        raise ValueError(f"Authentication failed: {msg}")
    except Exception as e:
        print(f"[AUTH] Error contacting server: {type(e).__name__}: {e}")
        raise ValueError(f"Could not reach auth server: {e}")

    custom_token = data.get("custom_token")
    username     = data.get("username")
    uuid         = data.get("uuid", "")

    if not custom_token or not username:
        raise ValueError("Incomplete response from auth server — missing token or username.")

    # Normalise UUID format just in case
    raw_uuid = uuid.replace("-", "")
    if len(raw_uuid) == 32:
        uuid = _format_uuid(raw_uuid)

    print(f"[AUTH] Successfully authenticated: {username} ({uuid})")

    return {
        "username":     username,
        "uuid":         uuid,
        "access_token": custom_token,   # custom_token from Flask server
    }


def login_microsoft(domain: str = "localhost"):
    """
    Opens the Aristois auth page in the system browser, starts a tiny local
    HTTP server to catch the redirect code, then submits it to the Flask
    server's /verify/submit endpoint.

    Returns the profile dict on success, or None on failure/timeout.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs
    import secrets

    CALLBACK_PORT = 9876
    redirect_uri  = f"https://auth.aristois.net/auth"

    # Generate a session_id that matches an existing verify session on the
    # Flask server. In the full launcher flow the session_id comes from
    # /verify/poll; here we create one locally for CLI / fallback use.
    session_id = secrets.token_hex(16)

    result_holder: list = []   # will hold the aristois code
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
                self.wfile.write(
                    b"<html><body><h2>Login complete. You may close this tab.</h2></body></html>"
                )
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

    aristois_code = result_holder[0]
    return _exchange_code_for_profile(aristois_code, session_id, domain)
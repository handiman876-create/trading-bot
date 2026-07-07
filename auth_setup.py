#!/usr/bin/env python3
"""
One-time TradeStation OAuth authorization.

Opens a browser to authorize this application, captures the redirect on a local
callback server, exchanges the authorization code for tokens, and writes
TS_REFRESH_TOKEN into .env so the bot can refresh access tokens unattended.

Prerequisites in .env (or the environment):
    TS_CLIENT_ID
    TS_CLIENT_SECRET
    TS_SANDBOX            (true/false - only affects which API the bot calls)

The redirect URI (default http://localhost:3000/) MUST be registered as an
allowed redirect URL for your API key in the TradeStation developer portal.
Override it with TS_REDIRECT_URI in .env if you registered a different one.

Usage:
    python3 auth_setup.py
"""

import http.server
import secrets
import socketserver
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import requests

import config

ENV_PATH = Path(__file__).with_name(".env")
_result: dict = {}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # Ignore unrelated requests (e.g. favicon) so we only stop on the redirect.
        if "code" not in qs and "error" not in qs:
            self.send_response(204)
            self.end_headers()
            return
        _result["code"]  = qs.get("code",  [None])[0]
        _result["state"] = qs.get("state", [None])[0]
        _result["error"] = qs.get("error", [None])[0]
        body = ("Authorization failed: " + _result["error"]
                if _result.get("error")
                else "Authorization complete. You can close this tab and return to the terminal.")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body><h3>{body}</h3></body></html>".encode())

    def log_message(self, *args):  # silence default request logging
        pass


def _redirect_port() -> int:
    parsed = urllib.parse.urlparse(config.TS_REDIRECT_URI)
    return parsed.port or 80


def _write_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def main() -> None:
    if not config.TS_CLIENT_ID or not config.TS_CLIENT_SECRET:
        print("ERROR: set TS_CLIENT_ID and TS_CLIENT_SECRET in .env first.")
        sys.exit(1)

    state = secrets.token_urlsafe(16)
    auth_url = config.TS_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id":     config.TS_CLIENT_ID,
        "redirect_uri":  config.TS_REDIRECT_URI,
        "audience":      config.TS_AUDIENCE,
        "scope":         config.TS_SCOPE,
        "state":         state,
    })

    httpd = socketserver.TCPServer(("", _redirect_port()), _CallbackHandler)
    print("Opening your browser to authorize TradeStation access...")
    print("If it does not open automatically, paste this URL into your browser:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)

    # Block until the browser hits our callback exactly once.
    while not _result:
        httpd.handle_request()
    httpd.server_close()

    if _result.get("error"):
        print(f"Authorization failed: {_result['error']}")
        sys.exit(1)
    if _result.get("state") != state:
        print("ERROR: OAuth state mismatch (possible CSRF). Aborting.")
        sys.exit(1)
    code = _result.get("code")
    if not code:
        print("ERROR: no authorization code returned.")
        sys.exit(1)

    print("Exchanging authorization code for tokens...")
    resp = requests.post(config.TS_TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "client_id":     config.TS_CLIENT_ID,
        "client_secret": config.TS_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  config.TS_REDIRECT_URI,
    }, timeout=15)
    resp.raise_for_status()
    tokens = resp.json()

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("ERROR: no refresh_token in response. Make sure the 'offline_access' "
              "scope is enabled for your API key.")
        print(tokens)
        sys.exit(1)

    _write_env("TS_REFRESH_TOKEN", refresh_token)
    print("\n[OK] Success - TS_REFRESH_TOKEN saved to .env")
    print("     You can now start the bot with: python3 main.py")


if __name__ == "__main__":
    main()

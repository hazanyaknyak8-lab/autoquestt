"""
GET /api/callback
Discord OAuth2 redirect target (Vercel).

Flow:
  1. Discord redirects here with ?code=...&state=...
  2. state -> uid is read (and deleted) from Upstash  (oauth:state:<state>)
  3. code is exchanged for tokens at Discord
  4. result is stored at oauth:done:<uid> for the bot's poll loop to pick up

Vercel env vars:
  CLIENT_ID, CLIENT_SECRET, REDIRECT_URI (optional),
  UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import html
import json
import os
import urllib.parse
import urllib.request
import urllib.error

CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET") or os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "https://erzaautoquest.vercel.app/api/callback")

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# Where to send the user after a successful link (the dashboard in index.html).
SITE_URL = os.environ.get("SITE_URL", "https://erzaautoquest.vercel.app/")

DISCORD_API = "https://discord.com/api/v10"
DONE_TTL = 300

# IMPORTANT: Discord sits behind Cloudflare, which blocks the default
# "Python-urllib/x.y" User-Agent with `error code: 1010`. Always send a real one.
USER_AGENT = "DiscordBot (https://erzaautoquest.vercel.app, 1.0)"


# ---------------------------------------------------------------------------
# Upstash
# ---------------------------------------------------------------------------

def _upstash(*cmd):
    req = urllib.request.Request(
        f"{UPSTASH_URL}/pipeline",
        data=json.dumps([[str(c) for c in cmd]]).encode(),
        headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        item = json.loads(r.read())[0]
    if item.get("error"):
        raise RuntimeError(f"upstash: {item['error']}")
    return item.get("result")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _discord_request(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        # Full detail goes to Vercel logs only, never to the visitor.
        print(f"[callback] discord http {e.code}: {body}")
        raise RuntimeError(f"Discord returned HTTP {e.code}")


def exchange_code(code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        f"{DISCORD_API}/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    return _discord_request(req)


def get_me(access_token: str) -> dict:
    req = urllib.request.Request(
        f"{DISCORD_API}/users/@me",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    return _discord_request(req)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>{title} — Omura</title>
<style>
  :root{{color-scheme:dark}}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:grid;place-items:center;
        background:radial-gradient(circle at 80% 90%,#1a0a0e,#07070d 60%);
        font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8e8f0}}
  .card{{width:min(420px,92vw);padding:32px;border-radius:24px;
        background:#0c0c16;border:1px solid #1e1e2e}}
  .icon{{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;
        font-size:22px;background:{soft};border:1px solid {line};margin-bottom:18px}}
  .tag{{display:inline-block;padding:3px 12px;border-radius:99px;font-size:11px;
        letter-spacing:.08em;color:{color};background:{soft};border:1px solid {line}}}
  h1{{margin:14px 0 8px;font-size:26px}}
  p{{margin:0;color:#9a9ab0;line-height:1.55;font-size:14px}}
  .foot{{margin-top:28px;font-size:11px;color:#55556a}}
</style></head><body>
<div class="card">
  <div class="icon">{icon}</div>
  <span class="tag">{tag}</span>
  <h1>{title}</h1>
  <p>{message}</p>
  <div class="foot">Omura Auto Quest — made by Dope · powered by Anomalous Labs</div>
</div></body></html>"""


def _render(ok: bool, title: str, message: str, redirect: str = "") -> bytes:
    if ok:
        color, soft, line, icon, tag = "#4ade80", "#0f2a1a", "#1d5a35", "✓", "SUCCESS"
    else:
        color, soft, line, icon, tag = "#f87171", "#2a0f12", "#5a1d24", "!", "ERROR"
    refresh = (
        f'<meta http-equiv="refresh" content="2;url={html.escape(redirect, quote=True)}">\n'
        if redirect else ""
    )
    return PAGE.format(
        refresh=refresh,
        title=html.escape(title), message=html.escape(message),
        color=color, soft=soft, line=line, icon=icon, tag=tag,
    ).encode()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]

        if q.get("error"):
            return self._page(400, False, "Authorization Cancelled",
                              "You cancelled the authorization. Run /link again to retry.")

        if not (code and state):
            return self._page(400, False, "Invalid Request",
                              "Missing code or state. Run /link in Discord to start again.")

        if not (CLIENT_ID and CLIENT_SECRET and UPSTASH_URL and UPSTASH_TOKEN):
            print("[callback] missing env vars")
            return self._page(500, False, "Server Misconfigured",
                              "The server is missing configuration. Please tell the bot owner.")

        # state -> uid (one use)
        try:
            uid = _upstash("GETDEL", f"oauth:state:{state}")
        except Exception as e:
            print(f"[callback] state lookup failed: {e}")
            return self._page(502, False, "Storage Error",
                              "Could not verify your session. Try /link again.")
        if not uid:
            return self._page(400, False, "Session Expired",
                              "This link expired or was already used. Run /link again.")

        # code -> tokens
        try:
            tok = exchange_code(code)
            access_token = tok.get("access_token")
            if not access_token:
                raise RuntimeError("no access_token in response")
            try:
                global_name = get_me(access_token).get("global_name") or "Unknown"
            except Exception:
                global_name = "Unknown"
        except Exception as e:
            print(f"[callback] exchange failed uid={uid}: {e}")
            return self._page(502, False, "OAuth Failed",
                              "Discord rejected the login. Run /link again. If it keeps failing, tell the bot owner.")

        # hand off to the bot
        try:
            _upstash("SET", f"oauth:done:{uid}", json.dumps({
                "access_token": access_token,
                "refresh_token": tok.get("refresh_token", ""),
                "global_name": global_name,
            }), "EX", DONE_TTL)
        except Exception as e:
            print(f"[callback] handoff failed uid={uid}: {e}")
            return self._page(502, False, "Storage Error",
                              "Login worked but it could not be saved. Try /link again.")

        self._page(200, True, "Linked",
                   f"Welcome, {global_name}. Auto-quest is starting. Taking you to the dashboard...",
                   redirect=SITE_URL)

    def _page(self, status, ok, title, message, redirect=""):
        body = _render(ok, title, message, redirect)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

# api/callback.py  — Vercel serverless function
"""
Handles Discord OAuth callback at https://erzaautoquest.vercel.app/api/callback

Flow:
  1. Discord redirects here with ?code=&state=
  2. We read state -> uid from Upstash (put there by generate_link_url)
  3. Exchange code for access_token + refresh_token with Discord
  4. Push result to Upstash key oauth:done:{uid} (TTL 300s)
  5. Bot polls that key, picks it up, starts heartbeat
  6. Render success/error HTML to user

Vercel env vars required:
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
  CLIENT_ID
  CLIENT_SECRET
  REDIRECT_URI   (must be https://erzaautoquest.vercel.app/api/callback)
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
import json
import os
import urllib.request
import urllib.error
import urllib.parse

UPSTASH_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
CLIENT_ID     = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
REDIRECT_URI  = os.environ.get("REDIRECT_URI", "https://erzaautoquest.vercel.app/api/callback")
DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
DISCORD_ME_URL    = "https://discord.com/api/v10/users/@me"


# ---------------------------------------------------------------------------
# Upstash helpers (sync urllib only — no aiohttp in Vercel serverless)
# ---------------------------------------------------------------------------

def _upstash(method: str, *args):
    """Single Upstash REST command. Returns result value or raises."""
    cmd = [method] + list(args)
    req = urllib.request.Request(
        f"{UPSTASH_URL}/pipeline",
        data=json.dumps([cmd]).encode(),
        headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        body = json.loads(r.read())
    # pipeline returns list of {result, error}
    item = body[0]
    if item.get("error"):
        raise RuntimeError(f"Upstash error: {item['error']}")
    return item.get("result")


def _get_state(state: str):
    """Returns uid string or None."""
    val = _upstash("GET", f"oauth:state:{state}")
    return val  # already a string or None


def _del_state(state: str):
    _upstash("DEL", f"oauth:state:{state}")


def _push_result(uid: str, payload: dict):
    """Store completed OAuth result for bot to pick up. TTL 300s."""
    _upstash("SET", f"oauth:done:{uid}", json.dumps(payload), "EX", "300")


# ---------------------------------------------------------------------------
# Discord HTTP helpers (sync)
# ---------------------------------------------------------------------------

def _exchange_code(code: str) -> dict:
    data = urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(
        DISCORD_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise RuntimeError(f"token exchange {e.code}: {body}")


def _get_me(access_token: str) -> dict:
    req = urllib.request.Request(
        DISCORD_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Page renderer
# ---------------------------------------------------------------------------

def _page(title: str, body_html: str, is_error: bool = False) -> str:
    accent = "#ff4747" if is_error else "#3fe0a5"
    icon_check = (
        '<svg viewBox="0 0 24 24" fill="none" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M20 6L9 17l-5-5"/></svg>'
    )
    icon_warn = (
        '<svg viewBox="0 0 24 24" fill="none" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94'
        'a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>'
    )
    icon = icon_warn if is_error else icon_check
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title} — Omura</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#08060f; --bg-well:#0d0a18; --accent:{accent};
    --ink:#f1edfb; --ink-mid:#a99ecb; --ink-low:#655c82; --line:#ffffff17;
    box-sizing:border-box;
    padding-top:env(safe-area-inset-top,0px);
    padding-bottom:env(safe-area-inset-bottom,0px);
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  html,body{{height:100%}}
  body{{
    font-family:'Inter',system-ui,sans-serif;
    color:var(--ink);background:var(--bg);
    min-height:100%;
    display:flex;align-items:center;justify-content:center;
    padding:32px 16px;
  }}
  .stage{{position:fixed;inset:0;z-index:-1;overflow:hidden}}
  .stage::before{{
    content:'';position:absolute;inset:-10%;
    background:
      radial-gradient(circle at 20% 20%,#4b2aa815 0%,transparent 50%),
      radial-gradient(circle at 80% 80%,{accent}18 0%,transparent 45%);
  }}
  .grid-fade{{
    position:absolute;inset:0;
    background-image:linear-gradient(to right,#ffffff08 1px,transparent 1px),
                     linear-gradient(to bottom,#ffffff08 1px,transparent 1px);
    background-size:42px 42px;
    -webkit-mask-image:radial-gradient(circle at 50% 40%,black 0%,transparent 65%);
            mask-image:radial-gradient(circle at 50% 40%,black 0%,transparent 65%);
  }}
  .card{{
    width:100%;max-width:420px;
    padding:40px 36px 32px;
    border-radius:20px;
    background:var(--bg-well);border:1px solid var(--line);
    box-shadow:0 32px 80px -20px #00000099;
    opacity:0;animation:rise .45s cubic-bezier(.2,.8,.2,1) forwards;
  }}
  @keyframes rise{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:none}}}}
  .icon{{
    width:52px;height:52px;border-radius:16px;
    background:{accent}22;border:1px solid {accent}44;
    display:flex;align-items:center;justify-content:center;
    margin-bottom:22px;
  }}
  .icon svg{{width:24px;height:24px;stroke:var(--accent)}}
  .pill{{
    display:inline-flex;align-items:center;gap:6px;
    background:{accent}18;border:1px solid {accent}33;
    border-radius:999px;padding:4px 12px;
    font-size:12px;font-weight:600;color:var(--accent);
    margin-bottom:18px;letter-spacing:.03em;text-transform:uppercase;
  }}
  h1{{font-family:'Sora',sans-serif;font-size:24px;font-weight:700;margin-bottom:10px;line-height:1.2}}
  p{{color:var(--ink-mid);font-size:14px;line-height:1.7;margin-bottom:10px}}
  p strong{{color:var(--ink);font-weight:600}}
  .steps{{
    margin:18px 0;padding:16px 18px;
    background:#ffffff07;border:1px solid var(--line);border-radius:12px;
    display:flex;flex-direction:column;gap:10px;
  }}
  .step{{display:flex;align-items:flex-start;gap:12px;font-size:13px;color:var(--ink-mid)}}
  .step-num{{
    min-width:22px;height:22px;border-radius:50%;
    background:{accent}22;border:1px solid {accent}44;
    color:var(--accent);font-size:11px;font-weight:700;
    display:flex;align-items:center;justify-content:center;flex-shrink:0;
    margin-top:1px;
  }}
  .step-txt{{line-height:1.5}}
  .step-txt strong{{color:var(--ink);font-weight:600}}
  code{{
    font-family:'JetBrains Mono',monospace;font-size:12px;
    color:#c3b3ff;background:#ffffff0a;padding:2px 7px;border-radius:5px;
  }}
  .foot{{margin-top:24px;font-size:11.5px;color:var(--ink-low)}}
  .divider{{height:1px;background:var(--line);margin:16px 0}}
</style>
</head>
<body>
<div class="stage"><div class="grid-fade"></div></div>
<div class="card">
  <div class="icon">{icon}</div>
  <div class="pill">{'Error' if is_error else 'Omura Auto Quest'}</div>
  <h1>{title}</h1>
  {body_html}
  <div class="foot">Omura Auto Quest — made by Dope · powered by Anomalous Labs</div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed   = urlparse(self.path)
        params   = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        code     = params.get("code")
        state    = params.get("state")
        error    = params.get("error")

        # --- user denied ---
        if error:
            self._html(_page(
                "Authorization Denied",
                "<p>You didn't authorize Omura. Close this tab and use <code>/link</code> again if you change your mind.</p>",
                is_error=True,
            ))
            return

        if not code:
            self._html(_page("Missing Code", "<p>No authorization code received. Try <code>/link</code> again.</p>", is_error=True))
            return

        if not state:
            self._html(_page("Missing State", "<p>Invalid callback. Use <code>/link</code> again.</p>", is_error=True))
            return

        # --- state lookup ---
        try:
            uid = _get_state(state)
        except Exception as e:
            self._html(_page("Server Error", f"<p>Redis lookup failed: <code>{e}</code></p>", is_error=True))
            return

        if not uid:
            self._html(_page(
                "Session Not Found",
                "<p>This link is invalid or already used. Use <code>/link</code> in Discord to get a fresh one.</p>",
                is_error=True,
            ))
            return

        _del_state(state)  # one-time use

        # --- exchange code ---
        try:
            token_data = _exchange_code(code)
        except Exception as e:
            self._html(_page("OAuth Failed", f"<p>Code exchange failed: <code>{e}</code><br>Try <code>/link</code> again.</p>", is_error=True))
            return

        access_token  = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token", "")

        if not access_token:
            self._html(_page("OAuth Failed", "<p>No access token returned. Try <code>/link</code> again.</p>", is_error=True))
            return

        # --- verify identity ---
        try:
            user = _get_me(access_token)
        except Exception as e:
            self._html(_page("Verification Failed", f"<p>Couldn't verify identity: <code>{e}</code></p>", is_error=True))
            return

        global_name = user.get("global_name") or user.get("username") or "Unknown"
        username    = user.get("username", "unknown")

        # --- push to Upstash for bot to pick up ---
        try:
            _push_result(uid, {
                "access_token":  access_token,
                "refresh_token": refresh_token,
                "username":      username,
                "global_name":   global_name,
                "discord_id":    user.get("id", ""),
            })
        except Exception as e:
            self._html(_page("Server Error", f"<p>Failed to store session: <code>{e}</code></p>", is_error=True))
            return

        # --- success page ---
        body = f"""
<p>Signed in as <strong>{global_name}</strong>.</p>
<div class="divider"></div>
<div class="steps">
  <div class="step"><div class="step-num">1</div><div class="step-txt"><strong>Auto-quest is starting</strong> — the bot picks up your session within seconds.</div></div>
  <div class="step"><div class="step-num">2</div><div class="step-txt">You'll get a <strong>DM</strong> when all quests are claimed.</div></div>
  <div class="step"><div class="step-num">3</div><div class="step-txt">Use <code>/quests</code> to check progress or <code>/unlink</code> to stop at any time.</div></div>
</div>
<p>You can close this tab.</p>
"""
        self._html(_page("You're linked ✓", body))

    def _html(self, body: str):
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass  # silence Vercel access log spam

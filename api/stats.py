"""
GET /api/stats
Reads bot:heartbeat, bot:linked_count and the last 7 days of
stats:active:<date> sets from Upstash, returns JSON for the dashboard.

Vercel env vars needed (same values the bot's .env uses):
  UPSTASH_REDIS_REST_URL
  UPSTASH_REDIS_REST_TOKEN
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import time
import datetime
import urllib.request
import urllib.error

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

ONLINE_WINDOW = 90  # seconds — must match stats.py's HEARTBEAT_TTL on the bot


def _pipeline(commands):
    req = urllib.request.Request(
        f"{UPSTASH_URL}/pipeline",
        data=json.dumps(commands).encode(),
        headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not (UPSTASH_URL and UPSTASH_TOKEN):
            self._send(500, {"error": "UPSTASH_REDIS_REST_URL/TOKEN not set on Vercel"})
            return

        today_dt = datetime.datetime.now(datetime.timezone.utc)
        last7_dates = [
            (today_dt - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(6, -1, -1)
        ]

        commands = [["GET", "bot:heartbeat"], ["GET", "bot:linked_count"]]
        commands += [["SCARD", f"stats:active:{d}"] for d in last7_dates]

        try:
            results = _pipeline(commands)
        except urllib.error.HTTPError as e:
            self._send(502, {"error": f"upstash http {e.code}"})
            return
        except Exception as e:
            self._send(502, {"error": str(e)})
            return

        heartbeat = results[0].get("result")
        linked_count = results[1].get("result")
        now = int(time.time())
        online = bool(heartbeat) and (now - int(heartbeat)) < ONLINE_WINDOW

        last7 = []
        for i, d in enumerate(last7_dates):
            count = results[2 + i].get("result") or 0
            last7.append({"date": d, "count": int(count)})

        self._send(200, {
            "online": online,
            "last_seen": int(heartbeat) if heartbeat else None,
            "linked_count": int(linked_count) if linked_count else 0,
            "today_active": last7[-1]["count"],
            "yesterday_active": last7[-2]["count"],
            "last7": last7,
        })

    def _send(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

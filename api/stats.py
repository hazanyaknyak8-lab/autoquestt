"""GET /api/stats - bot status + active users for the dashboard."""

from http.server import BaseHTTPRequestHandler
import datetime
import json
import os
import time
import urllib.error
import urllib.request

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
ONLINE_WINDOW = 90  # seconds


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


def _num(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not (UPSTASH_URL and UPSTASH_TOKEN):
            return self._send(500, {"error": "UPSTASH_REDIS_REST_URL/TOKEN not set on Vercel"})

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        dates = [(now_dt - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        cmds = [["GET", "bot:heartbeat"], ["GET", "bot:linked_count"]]
        cmds += [["SCARD", f"stats:active:{d}"] for d in dates]

        try:
            results = _pipeline(cmds)
        except urllib.error.HTTPError as e:
            return self._send(502, {"error": f"upstash http {e.code}"})
        except Exception as e:
            return self._send(502, {"error": str(e)})

        heartbeat = _num(results[0].get("result"), None)
        linked = _num(results[1].get("result"))
        online = heartbeat is not None and (int(time.time()) - heartbeat) < ONLINE_WINDOW
        last7 = [{"date": d, "count": _num(results[2 + i].get("result"))} for i, d in enumerate(dates)]

        self._send(200, {
            "online": online,
            "last_seen": heartbeat,
            "linked_count": linked,
            "today_active": last7[-1]["count"],
            "yesterday_active": last7[-2]["count"],
            "last7": last7,
        })

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

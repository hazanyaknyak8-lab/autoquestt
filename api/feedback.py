"""
GET /api/feedback
Reads the last 20 feedback entries + running rating average from Upstash.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.request
import urllib.error

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


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

        try:
            results = _pipeline([
                ["LRANGE", "feedback:list", "0", "19"],
                ["GET", "feedback:rating_sum"],
                ["GET", "feedback:rating_count"],
            ])
        except urllib.error.HTTPError as e:
            self._send(502, {"error": f"upstash http {e.code}"})
            return
        except Exception as e:
            self._send(502, {"error": str(e)})
            return

        raw_list = results[0].get("result") or []
        entries = []
        for item in raw_list:
            try:
                entries.append(json.loads(item))
            except Exception:
                continue

        rating_sum = int(results[1].get("result") or 0)
        rating_count = int(results[2].get("result") or 0)
        average = round(rating_sum / rating_count, 2) if rating_count else 0

        self._send(200, {
            "average": average,
            "count": rating_count,
            "recent": entries,
        })

    def _send(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

#!/usr/bin/env python3
"""Inven 2097 keyword proxy for the Telegram mini-app hub.

Why this exists: Inven sends no CORS headers, so a page running in the
user's browser cannot read board 2097 directly — the browser blocks the
cross-origin read. A server-to-server request is not subject to CORS, so
this tiny server fetches the board, filters titles, and serves JSON with
CORS enabled, letting the mini app fetch live data.

Stdlib only — no dependencies, no secrets, no database.
Endpoints:
  GET /api/inven  -> {"updated_at", "total", "matched": [{"id","title","url"}]}
  GET /health     -> "ok"
  GET /           -> tiny status page
Deploy: Render (free tier) — see render.yaml / README.md.
"""
import html
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOARD_URL = "https://www.inven.co.kr/board/webzine/2097"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
KEYWORDS = ("ㅇㅎ", "ㅎㅂ", "약후", "후방")
CACHE_TTL = 60  # seconds; Inven page 1 changes slowly, don't hammer it


def matches(title):
    return any(k in title for k in KEYWORDS)


def fetch_posts():
    req = urllib.request.Request(BOARD_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        src = resp.read().decode("utf-8", errors="replace")
    posts = {}
    # Two row layouts on the page: thumbnail rows carry the title in
    # <span class="txt">, plain rows in <b> inside a.subject-link.
    anchor_pat = re.compile(
        r'<a[^>]*href="https?://www\.inven\.co\.kr/board/webzine/2097/(\d+)[^"]*"[^>]*>(.*?)</a>', re.S)
    for pid, inner in anchor_pat.findall(src):
        m = re.search(r'<span class="txt">(.*?)</span>', inner, re.S)
        if not m:
            m = re.search(r'<b>(.*?)</b>', inner, re.S)
        if m:
            raw = m.group(1)
        else:
            raw = re.sub(r"^\[[^\]]+\]\s*", "", re.sub(r"<[^>]+>", "", inner))
        title = html.unescape(re.sub(r"<[^>]+>", "", raw))
        title = re.sub(r"\s+", " ", title).strip()
        if title and pid not in posts:
            posts[pid] = title
    return posts


_cache = {"at": 0.0, "payload": None}
_cache_lock = threading.Lock()


def get_payload():
    """Cached (total, matched) -> JSON-ready dict. Serves stale data on error."""
    now = time.time()
    with _cache_lock:
        if _cache["payload"] is not None and now - _cache["at"] < CACHE_TTL:
            return _cache["payload"], False
    try:
        posts = fetch_posts()
        matched = [{"id": pid, "title": t,
                    "url": f"https://www.inven.co.kr/board/webzine/2097/{pid}"}
                   for pid, t in posts.items() if matches(t)]
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "total": len(posts),
            "matched": matched,
            "stale": False,
        }
        with _cache_lock:
            _cache["at"] = now
            _cache["payload"] = payload
        return payload, False
    except Exception as e:
        with _cache_lock:
            old = _cache["payload"]
        if old is not None:
            stale = dict(old)
            stale["stale"] = True
            stale["error"] = str(e)
            return stale, False
        return {"updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "total": 0, "matched": [], "stale": False, "error": str(e)}, True


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/api/inven", "/api/inven/"):
            payload, failed = get_payload()
            self._send_json(502 if failed else 200, payload)
        elif self.path in ("/health", "/health/"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", ""):
            body = ("inven-proxy is up.\n"
                    "GET /api/inven -> matched posts as JSON (CORS enabled).\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass  # keep Render logs quiet


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"inven-proxy listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()

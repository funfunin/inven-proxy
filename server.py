#!/usr/bin/env python3
"""Inven 2097 keyword proxy for the Telegram mini-app hub.

Why this exists: Inven sends no CORS headers, so a page running in the
user's browser cannot read board 2097 directly — the browser blocks the
cross-origin read. A server-to-server request is not subject to CORS, so
this tiny server fetches the board, filters titles, and serves JSON with
CORS enabled, letting the mini app fetch live data.

Stdlib only — no dependencies, no secrets, no database.
Endpoints:
  GET /api/inven  -> {"updated_at", "total", "matched": [{"id","title","url",
                    "media": [{"type": "video"|"photo", "src": "<url>"}]}]}
                    Media is extracted from each post's own page: <video>
                    data-src/src for videos, <img> src on upload*.inven.co.kr
                    under /bbs/ for photos. A post with no media gets "media": [].
  GET /media?url=<urlencoded Inven CDN URL> -> the media bytes, streamed.
                    Only https://upload*.inven.co.kr URLs are allowed (anything
                    else -> 400), so this cannot be abused as an open proxy.
                    The client's Range header is forwarded and the origin's
                    status (200/206) and Content-Type/Length/Range headers
                    are passed through, so <video> seeking works. The
                    browser's Referer is NOT forwarded — Inven's CDN
                    rejects embedded requests that carry a foreign Referer,
                    which is why the hub must load remote media via this
                    endpoint instead of directly. HEAD works the same way
                    (headers only). Origin failure -> 502.
  GET /health     -> "ok"
  GET /           -> tiny status page
Deploy: Render (free tier) — see render.yaml / README.md.
"""
import concurrent.futures
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
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


# --- per-post media -------------------------------------------------------
# The board list carries no media, so each matched post's own page is
# fetched and its body scanned for videos/photos. A post's content never
# changes, so media is cached per post id indefinitely.
POST_TIMEOUT = 8  # seconds per post-page fetch
MAX_VIDEOS = 5
MAX_PHOTOS = 4
ENRICH_BUDGET = 10  # seconds for the whole media step, all posts together

_TAG_RE = re.compile(r"<(video|img)\b[^>]*>", re.I)
_UPLOAD_IMG_RE = re.compile(r"^https://upload\d*\.inven\.co\.kr/[^\"']*/bbs/", re.I)

_media_cache = {}  # pid -> [{"type": "video"|"photo", "src": url}]
_media_lock = threading.Lock()


def _attr(tag, name):
    m = re.search(r"\b" + name + r'="([^"]*)"', tag, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def extract_media(page_src):
    """Videos/photos inside the post body, in page order.

    The body lives in <div id="powerbbsContent">; scoping to it keeps out
    header/sidebar thumbnails and comment avatars, which also sit on the
    upload hosts. If that wrapper is missing (markup change), the whole
    page is scanned instead. Videos come from <video> data-src (src as
    fallback); photos from <img> src on upload*.inven.co.kr under /bbs/.
    """
    start = page_src.find('<div id="powerbbsContent">')
    if start != -1:
        end = page_src.find('<div class="contentsBotMenu">', start)
        body = page_src[start:end if end != -1 else len(page_src)]
    else:
        body = page_src
    media, seen, n_vid, n_img = [], set(), 0, 0
    for m in _TAG_RE.finditer(body):
        if n_vid >= MAX_VIDEOS and n_img >= MAX_PHOTOS:
            break
        tag = m.group(0)
        if m.group(1).lower() == "video":
            if n_vid >= MAX_VIDEOS:
                continue
            url = _attr(tag, "data-src") or _attr(tag, "src")
            if not url.startswith(("http://", "https://")):
                continue
            kind = "video"
        else:
            if n_img >= MAX_PHOTOS:
                continue
            url = _attr(tag, "src")
            if not _UPLOAD_IMG_RE.match(url):
                continue
            kind = "photo"
        if url in seen:
            continue
        seen.add(url)
        media.append({"type": kind, "src": url})
        if kind == "video":
            n_vid += 1
        else:
            n_img += 1
    return media


def fetch_post_media(pid):
    """Media for one post. Any failure -> [] — one bad post must never
    break the whole /api/inven response."""
    with _media_lock:
        if pid in _media_cache:
            return _media_cache[pid]
    media = []
    try:
        req = urllib.request.Request(
            f"https://www.inven.co.kr/board/webzine/2097/{pid}", headers=UA)
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            page = resp.read().decode("utf-8", errors="replace")
        media = extract_media(page)
    except Exception as e:
        print(f"fetch_post_media {pid} failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        media = []
    if media:
        # Only cache non-empty results: a failed/slow first fetch must not
        # poison the cache with [] forever — the next refresh retries.
        with _media_lock:
            _media_cache[pid] = media
    return media


def enrich_media(matched):
    """Attach a "media" array to each matched item. Post pages are fetched
    in parallel (max 8 at once); posts still unfinished when the budget
    runs out just get "media": []."""
    if not matched:
        return matched
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_post_media, m["id"]): m for m in matched}
        done, _ = concurrent.futures.wait(futs, timeout=ENRICH_BUDGET)
        for fut, item in futs.items():
            try:
                item["media"] = fut.result() if fut in done else []
            except Exception:
                item["media"] = []
    return matched


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
        enrich_media(matched)  # adds "media" per post; failures -> []
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


# --- media passthrough ----------------------------------------------------
# Inven's CDN rejects embedded media requests whose Referer is a foreign
# origin (HTTP 403), so the hub cannot load remote videos/photos directly
# even with referrerpolicy="no-referrer". This endpoint fetches the bytes
# server-side (no browser headers at all) and streams them back, so the
# browser only ever talks to this proxy.
MEDIA_TIMEOUT = 20  # seconds per origin fetch
MEDIA_CHUNK = 65536  # 64KB streaming chunks; video bytes are never buffered
_MEDIA_HOST_RE = re.compile(r"^upload\d*\.inven\.co\.kr$", re.I)
_MEDIA_PASSTHROUGH = ("Content-Type", "Content-Length", "Content-Range",
                       "Accept-Ranges")


def _media_origin_headers(req_headers):
    """Headers forwarded to the origin: our UA plus the client's Range
    (needed for <video> seeking). The browser's Referer is deliberately
    NOT forwarded — that is the whole point of this endpoint."""
    headers = dict(UA)
    rng = req_headers.get("Range")
    if rng:
        headers["Range"] = rng
    return headers


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_media(self, head_only):
        """GET/HEAD /media?url=<urlencoded>. Streams the origin bytes."""
        try:
            qs = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            target = (qs.get("url") or [""])[0]
            u = urllib.parse.urlparse(target)
            if not target or u.scheme != "https" or not _MEDIA_HOST_RE.match(u.netloc or ""):
                self._send_json(400, {"error": "url must be an https://upload*.inven.co.kr URL"})
                return
        except Exception:
            self._send_json(400, {"error": "bad url parameter"})
            return
        method = "HEAD" if head_only else "GET"
        req = urllib.request.Request(
            target, headers=_media_origin_headers(self.headers), method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=MEDIA_TIMEOUT)
        except urllib.error.HTTPError as e:
            # Origin answered with an error status (e.g. 403/404): pass the
            # status through, empty body. A video element treats it as a
            # failed load, same as a direct request would.
            self.send_response(e.code)
            if e.headers.get("Content-Type"):
                self.send_header("Content-Type", e.headers.get("Content-Type"))
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        except Exception:
            self._send_json(502, {"error": "origin fetch failed"})
            return
        try:
            self.send_response(resp.status)
            for name in _MEDIA_PASSTHROUGH:
                val = resp.headers.get(name)
                if val is not None:
                    self.send_header(name, val)
            self._cors()
            self.end_headers()
            if not head_only:
                while True:
                    chunk = resp.read(MEDIA_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; nothing to do
        finally:
            resp.close()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/media":
            self._serve_media(head_only=True)
        else:
            self.send_response(404)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

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
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/media":
            self._serve_media(head_only=False)
        elif parsed.path in ("/api/inven", "/api/inven/"):
            payload, failed = get_payload()
            self._send_json(502 if failed else 200, payload)
        elif parsed.path in ("/health", "/health/"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path in ("/", ""):
            body = ("inven-proxy is up.\n"
                    "GET /api/inven -> matched posts as JSON (CORS enabled).\n"
                    "GET /media?url=<Inven CDN URL> -> streamed media bytes.\n").encode("utf-8")
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

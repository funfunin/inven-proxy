#!/usr/bin/env python3
"""Inven 2097 keyword proxy for the Telegram mini-app hub.

Why this exists: Inven sends no CORS headers, so a page running in the
user's browser cannot read board 2097 directly — the browser blocks the
cross-origin read. A server-to-server request is not subject to CORS, so
this tiny server fetches the board, filters titles, and serves JSON with
CORS enabled, letting the mini app fetch live data.

Stdlib only — no dependencies, no secrets, no database.
Endpoints:
  GET /api/inven[?q=<keyword>] -> {"updated_at", "total", "matched":
                    [{"id","title","url", "media": [{"type": "video"|"photo",
                    "src": "<url>"}]}]}. Without q: titles containing ㅇㅎ/ㅎㅂ/
                    약후/후방 on the board list page 1. With q: Inven's own
                    native board search (name=subjcont = title+content,
                    whole board history, newest first), capped at 20 posts;
                    "total" is then the number of matched posts returned.
                    The 60s list cache is per query (keyed by the
                    normalized query), so a search never returns another
                    query's cached list.
                    Media is extracted from each post's own page: <video>
                    data-src/src for videos, <img> src on upload*.inven.co.kr
                    under /bbs/ for photos. A post with no media gets "media": [].
  GET /api/inven?all=1 -> same shape, but ALL page-1 posts that have
                    >=1 media item (photo or video, any type), newest
                    first, capped at 40; "total" is the number of
                    media posts BEFORE the cap. Own 60s cache, separate
                    from the feed/search caches; stale-on-error fallback.
  GET /api/x?q=<keyword>&limit=10 -> {"updated_at", "total", "matched":
                    [{"id","text","author":{"name","handle","avatar"},
                    "created_at","url","media":[{"type":"photo"|"video",
                    "src":"<url>"}]}]}. Keyword search of public X posts
                    via Weir (POST /v1/x/search, key from the WEIR_API_KEY
                    env var — the key is never in this file). X media URLs
                    (pbs.twimg.com / video.twimg.com) are returned directly:
                    they allow hotlinking, so the hub loads them without
                    /media. Per-query 60s cache, min 2s between upstream
                    calls, limit capped at 10. Missing key -> clean error
                    JSON (502), never a crash, never a leak.
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
SEARCH_URL = ("https://www.inven.co.kr/board/webzine/2097"
              "?query=list&p={page}&sterm=&name=subjcont&keyword={keyword}")
SEARCH_CAP = 20  # max search results returned; search page 1 holds ~25 rows
ALL_CAP = 40  # max ?all=1 results returned (page 1 holds ~63 posts); raised from 20 so the hub's shuffled 전체 pool is bigger
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
KEYWORDS = ("ㅇㅎ", "ㅎㅂ", "약후", "후방")
CACHE_TTL = 60  # seconds; Inven page 1 changes slowly, don't hammer it


def matches(title):
    return any(k in title for k in KEYWORDS)


def _title_from_anchor(inner):
    """Post title from a subject-link anchor's inner HTML: thumbnail rows
    carry it in <span class="txt">, plain rows in <b>."""
    m = re.search(r'<span class="txt">(.*?)</span>', inner, re.S)
    if not m:
        m = re.search(r'<b>(.*?)</b>', inner, re.S)
    if m:
        raw = m.group(1)
    else:
        raw = re.sub(r"^\[[^\]]+\]\s*", "", re.sub(r"<[^>]+>", "", inner))
    title = html.unescape(re.sub(r"<[^>]+>", "", raw))
    return re.sub(r"\s+", " ", title).strip()


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
        title = _title_from_anchor(inner)
        if title and pid not in posts:
            posts[pid] = title
    return posts


def _parse_search_page(src):
    """Result rows from a native board-search page: ONLY the rows of the
    list table (<table class="thumbnail"> <tbody>), excluding pinned
    notice rows (class "notice ..."). The page also carries a hot-posts
    widget (ul.site-list) and the notices with board links — those are
    NOT search results, so a whole-page anchor scan (what fetch_posts
    does) must not be used here: verified 2026-11-24, the unscoped scan
    returned 44 links for keyword=비키니 while the list table holds 25
    real results + 3 notices."""
    m = re.search(r'<table class="thumbnail">.*?<tbody>(.*?)</tbody>', src, re.S)
    if not m:
        return {}
    anchor_pat = re.compile(
        r'<a[^>]*href="https?://www\.inven\.co\.kr/board/webzine/2097/(\d+)[^"]*"[^>]*>(.*?)</a>', re.S)
    posts = {}
    for row in re.finditer(r'<tr class="([^"]*)">(.*?)</tr>', m.group(1), re.S):
        if "notice" in row.group(1):
            continue
        for pid, inner in anchor_pat.findall(row.group(2)):
            title = _title_from_anchor(inner)
            if title and pid not in posts:
                posts[pid] = title
    return posts


def fetch_search_posts(query):
    """Native board search (title+content, whole board history, newest
    first). Page 1 first; if it yields fewer than SEARCH_CAP results,
    also try p=2 and append (dedupe by id). Capped at SEARCH_CAP.
    NOTE (verified 2026-11-24): Inven search does not paginate with p= —
    p=2 comes back with an empty list ("작성된 게시글이 없습니다");
    deeper history needs the page's '다음검색' sterm cursor, which this
    does not follow. p=1 alone returns up to ~25 rows, so the cap is
    what binds, not pagination."""
    keyword = urllib.parse.quote(query, safe="")
    posts = {}
    for page in (1, 2):
        url = SEARCH_URL.format(page=page, keyword=keyword)
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            src = resp.read().decode("utf-8", errors="replace")
        for pid, title in _parse_search_page(src).items():
            if pid not in posts:
                posts[pid] = title
        if len(posts) >= SEARCH_CAP:
            break
    return dict(list(posts.items())[:SEARCH_CAP])


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


def enrich_media(matched, max_workers=8):
    """Attach a "media" array to each matched item. Post pages are fetched
    in parallel; posts still unfinished when the budget runs out just get
    "media": []. The pool is shut down WITHOUT waiting (wait=False,
    cancel_futures=True) — otherwise the response would block until the
    slowest straggler finishes, far past the budget (verified: 63 posts
    took ~35s end-to-end with a 10s budget before this fix)."""
    if not matched:
        return matched
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {pool.submit(fetch_post_media, m["id"]): m for m in matched}
        done, _ = concurrent.futures.wait(futs, timeout=ENRICH_BUDGET)
        for fut, item in futs.items():
            try:
                item["media"] = fut.result() if fut in done else []
            except Exception:
                item["media"] = []
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return matched


_caches = {}  # normalized-query key (None = default feed) -> {"at", "payload"}
_cache_lock = threading.Lock()
_MAX_CACHE_KEYS = 32


def _normalize_query(query):
    """Empty/missing/whitespace-only query -> None (the default feed).
    Otherwise the stripped query, casefolded for the cache key."""
    q = (query or "").strip()
    return q.casefold() if q else None


def _matches_query(title, qkey, qraw):
    """Default feed: the ㅇㅎ/ㅎㅂ keyword filter. A search query: the
    query as a substring of the title (case-insensitive for Latin
    letters via casefold; Korean substring as-is)."""
    if qkey is None:
        return matches(title)
    return qraw in title.casefold()


def get_payload(query=None):
    """Cached (total, matched) -> JSON-ready dict. Serves stale data on error.
    The cache is per query: each normalized query has its own entry and
    timestamp, so a search for "A" can never return the cached list for
    "B" or for the default feed."""
    qkey = _normalize_query(query)
    qraw = (query or "").strip().casefold() if qkey is not None else ""
    now = time.time()
    with _cache_lock:
        entry = _caches.get(qkey)
        if entry is not None and now - entry["at"] < CACHE_TTL:
            return entry["payload"], False
    try:
        if qkey is not None:
            # Search: Inven's native board search already matched
            # title+content across the whole board history — every row
            # returned is a match, no client-side filtering. total is
            # the number of matched posts returned (capped at 20).
            posts = fetch_search_posts(query.strip())
            matched = [{"id": pid, "title": t,
                        "url": f"https://www.inven.co.kr/board/webzine/2097/{pid}"}
                       for pid, t in posts.items()]
            total = len(matched)
        else:
            posts = fetch_posts()
            matched = [{"id": pid, "title": t,
                        "url": f"https://www.inven.co.kr/board/webzine/2097/{pid}"}
                       for pid, t in posts.items() if _matches_query(t, qkey, qraw)]
            total = len(posts)
        enrich_media(matched)  # adds "media" per post; failures -> []
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "total": total,
            "matched": matched,
            "stale": False,
        }
        with _cache_lock:
            if len(_caches) >= _MAX_CACHE_KEYS:
                oldest = min(_caches, key=lambda k: _caches[k]["at"])
                del _caches[oldest]
            _caches[qkey] = {"at": now, "payload": payload}
        return payload, False
    except Exception as e:
        with _cache_lock:
            entry = _caches.get(qkey)
            old = entry["payload"] if entry is not None else None
        if old is not None:
            stale = dict(old)
            stale["stale"] = True
            stale["error"] = str(e)
            return stale, False
        return {"updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "total": 0, "matched": [], "stale": False, "error": str(e)}, True


# --- ?all=1: every media post on page 1 ---------------------------------
_all_cache = {"at": 0.0, "payload": None}  # single-entry cache, own key


def get_all_payload():
    """All page-1 posts that have >=1 media item, newest first, capped
    at ALL_CAP. Same enrichment (fetch_post_media, ENRICH_BUDGET) and
    same response shape as get_payload, with 24 workers so most of the
    ~63 post pages finish inside the budget; "total" is the number of
    media posts BEFORE the cap. Own 60s cache (separate from the
    feed/search caches — a search for "all" can never collide with
    this), stale on error. Posts whose media fetch is still unfinished
    when the budget runs out get "media": [] and are dropped from THIS
    list — the next refresh retries them (only non-empty media is
    cached, and finished posts answer from that cache instantly)."""
    now = time.time()
    with _cache_lock:
        if _all_cache["payload"] is not None and now - _all_cache["at"] < CACHE_TTL:
            return _all_cache["payload"], False
    try:
        posts = fetch_posts()
        items = [{"id": pid, "title": t,
                  "url": f"https://www.inven.co.kr/board/webzine/2097/{pid}"}
                 for pid, t in posts.items()]
        enrich_media(items, max_workers=24)  # adds "media" per post; unfinished -> []
        with_media = [m for m in items if m.get("media")]
        # Newest first: post ids are assigned sequentially, so a higher
        # id is a newer post. (The whole-page anchor scan does NOT come
        # back in list order — widget links interleave — so sort here
        # instead of trusting fetch_posts' order. 24 workers: a post
        # page takes ~3s from here, so 8 workers would only finish ~25
        # of the ~63 pages inside the 10s budget.)
        with_media.sort(key=lambda m: int(m["id"]), reverse=True)
        total = len(with_media)
        matched = with_media[:ALL_CAP]
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "total": total,
            "matched": matched,
            "stale": False,
        }
        with _cache_lock:
            _all_cache["at"] = now
            _all_cache["payload"] = payload
        return payload, False
    except Exception as e:
        with _cache_lock:
            old = _all_cache["payload"]
        if old is not None:
            stale = dict(old)
            stale["stale"] = True
            stale["error"] = str(e)
            return stale, False
        return {"updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "total": 0, "matched": [], "stale": False, "error": str(e)}, True


# --- X search via Weir ----------------------------------------------------
# POST /v1/x/search (docs.weirapp.io/reference/x/searchX, read 2026-11-24).
# Request: {"query": str, "sort": "latest", "limit": 1..100}; auth
# Authorization: Bearer <key>. Price 1 credit/call + 1/post; the call
# counts as 2 against the per-minute rate limit (free tier: 30/min).
# Response: {"data": [{id, text, url, lang, createdAt, author:
# {id, username, name, verified, followersCount, profileImageUrl},
# likeCount, retweetCount, replyCount, viewCount, media: [{id, type:
# "photo"|"video"|"animated_gif", url, previewImageUrl}], ...}],
# "meta": {"nextCursor": ...}}. media[].url is the original upload for a
# photo and the highest-bitrate MP4 for a video/GIF; null when X offers
# only a stream (then the item is dropped — the post still shows).
WEIR_API_URL = os.environ.get(
    "WEIR_API_URL", "https://api.weirapp.io/v1/x/search")
WEIR_TIMEOUT = 15  # seconds per upstream call
X_MIN_INTERVAL = 2.0  # seconds between upstream Weir calls (pacing)
X_CACHE_TTL = 60
X_LIMIT_MAX = 10
X_LIMIT_DEFAULT = 10

_x_caches = {}  # (query, limit) -> {"at", "payload"}
_x_last_call = 0.0
_x_lock = threading.Lock()


def _weir_media_of(raw):
    """Media array from a Weir post, defensively: the documented
    media[] shape first, then top-level photos/videos lists (other
    Weir endpoints / older shapes), then single photo/video URL
    fields. Anything without a usable URL is dropped; a video whose
    only URL is a poster frame is dropped too (a <video> cannot play
    a still image). animated_gif is a video (mp4)."""
    media, seen = [], set()

    def add(kind, src, poster=""):
        if not src or not isinstance(src, str):
            return
        src = src.strip()
        if not src.startswith(("http://", "https://")) or src in seen:
            return
        seen.add(src)
        item = {"type": kind, "src": src}
        if poster and poster.startswith(("http://", "https://")):
            item["poster"] = poster
        media.append(item)

    items = raw.get("media")
    if isinstance(items, list):
        for m in items:
            if not isinstance(m, dict):
                continue
            t = str(m.get("type") or "").lower()
            url = m.get("url") or ""
            poster = m.get("previewImageUrl") or ""
            if t == "photo":
                add("photo", url)
            elif t in ("video", "animated_gif"):
                add("video", url, poster)
            elif url:
                # Unknown type with a URL: treat .mp4 as video, else photo.
                add("video" if url.lower().split("?")[0].endswith(".mp4") else "photo",
                    url, poster)
    for key in ("photos", "images"):
        val = raw.get(key)
        if isinstance(val, list):
            for u in val:
                add("photo", u if isinstance(u, str) else
                    (u.get("url") if isinstance(u, dict) else ""))
    for key in ("videos",):
        val = raw.get(key)
        if isinstance(val, list):
            for u in val:
                add("video", u if isinstance(u, str) else
                    (u.get("url") if isinstance(u, dict) else ""))
    return media


def _normalize_weir_post(raw):
    """One Weir post -> the hub's shape. Missing fields degrade to
    empty strings / [] — a malformed post never breaks the list."""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "")
    if not pid:
        return None
    author = raw.get("author") or {}
    if not isinstance(author, dict):
        author = {}
    handle = str(author.get("username") or "")
    name = str(author.get("name") or handle or "")
    url = str(raw.get("url") or "")
    if not url:
        url = f"https://x.com/i/status/{pid}"
    return {
        "id": pid,
        "text": str(raw.get("text") or ""),
        "author": {
            "name": name,
            "handle": handle,
            "avatar": str(author.get("profileImageUrl") or ""),
        },
        "created_at": str(raw.get("createdAt") or ""),
        "url": url,
        "media": _weir_media_of(raw),
    }


def fetch_weir_search(query, limit):
    """POST /v1/x/search. Raises RuntimeError("WEIR_API_KEY is not
    configured") when the env var is missing — the caller turns that
    into a clean error JSON; the key itself is never logged or
    returned. WEIR_API_URL is overridable by env for local testing
    against a mock server."""
    key = os.environ.get("WEIR_API_KEY") or ""
    if not key:
        raise RuntimeError("WEIR_API_KEY is not configured")
    body = json.dumps({"query": query, "sort": "latest",
                       "limit": limit}).encode("utf-8")
    req = urllib.request.Request(
        WEIR_API_URL, data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=WEIR_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    posts = data.get("data") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        raise RuntimeError("unexpected Weir response shape")
    return [_normalize_weir_post(p) for p in posts if _normalize_weir_post(p)]


def get_x_payload(query, limit):
    """Cached X search -> (payload, failed). Same contract as
    get_payload: fresh cache hit returns immediately; on upstream
    error a stale cached payload is served with stale=True; with no
    cache at all the error payload is returned and failed=True (the
    handler then answers 502). Upstream calls are paced to at most
    one per X_MIN_INTERVAL seconds."""
    global _x_last_call
    q = (query or "").strip()
    if not q:
        return {"updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "total": 0, "matched": [], "stale": False,
                "error": "empty query"}, True
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = X_LIMIT_DEFAULT
    limit = max(1, min(X_LIMIT_MAX, limit))
    key = (q.casefold(), limit)
    now = time.time()
    with _x_lock:
        entry = _x_caches.get(key)
        if entry is not None and now - entry["at"] < X_CACHE_TTL:
            return entry["payload"], False
    try:
        # Pace upstream calls: at most one per X_MIN_INTERVAL seconds.
        # (Wait outside the lock so other requests are not stalled.)
        with _x_lock:
            wait = X_MIN_INTERVAL - (time.time() - _x_last_call)
        if wait > 0:
            time.sleep(wait)
        matched = fetch_weir_search(q, limit)
        with _x_lock:
            _x_last_call = time.time()
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "total": len(matched),
            "matched": matched,
            "stale": False,
        }
        with _x_lock:
            if len(_x_caches) >= _MAX_CACHE_KEYS:
                oldest = min(_x_caches, key=lambda k: _x_caches[k]["at"])
                del _x_caches[oldest]
            _x_caches[key] = {"at": now, "payload": payload}
        return payload, False
    except Exception as e:
        with _x_lock:
            entry = _x_caches.get(key)
            old = entry["payload"] if entry is not None else None
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
            qs = urllib.parse.parse_qs(parsed.query)
            if "1" in qs.get("all", []):
                payload, failed = get_all_payload()
            else:
                q = (qs.get("q") or [""])[0]
                payload, failed = get_payload(q)
            self._send_json(502 if failed else 200, payload)
        elif parsed.path in ("/api/x", "/api/x/"):
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0]
            limit = (qs.get("limit") or [""])[0]
            payload, failed = get_x_payload(q, limit)
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
                    "GET /api/inven?q=<keyword> -> native board search (title+content, whole board, newest 20).\n"
                    "GET /api/inven?all=1 -> all page-1 posts with media (newest 40).\n"
                    "GET /api/x?q=<keyword>&limit=10 -> X keyword search via Weir (newest 10).\n"
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

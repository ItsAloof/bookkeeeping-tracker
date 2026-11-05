#!/usr/bin/env python3
"""
Bookkeeping Tracker server.

A small, dependency-free WSGI app that:
  * serves the tracker page (index.html),
  * serves a cached copy of the SQLite engine (sql.js) so the page doesn't need a CDN,
  * stores the tracker's SQLite database on disk and lets the page load/save it
    (GET/PUT /api/db), with conflict detection, atomic writes and rolling backups,
  * protects everything with a password (HTTP Basic auth) unless it's only
    reachable from this computer.

Run locally:        python app.py
Run on a host:      TRACKER_PASSWORD=... python app.py --host 0.0.0.0 --port 8080
Any WSGI server:    point it at `app:application`

Environment variables:
  TRACKER_DATA_DIR   where the database and backups live   (default: ./data)
  TRACKER_PASSWORD   password for HTTP Basic auth           (default: none = this computer only)
  TRACKER_USER       username for HTTP Basic auth           (default: tracker)
  TRACKER_BACKUPS    how many backups to keep               (default: 60)
  HOST / PORT        bind address when run directly         (default: 127.0.0.1 / 8000)
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TRACKER_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "tracker.db"
BACKUP_DIR = DATA_DIR / "backups"
VENDOR_DIR = DATA_DIR / "vendor"
INDEX_HTML = ROOT / "index.html"

PASSWORD = os.environ.get("TRACKER_PASSWORD", "")
USERNAME = os.environ.get("TRACKER_USER", "tracker")
KEEP_BACKUPS = int(os.environ.get("TRACKER_BACKUPS", "60"))
BACKUP_EVERY = dt.timedelta(minutes=30)       # at most one backup per half hour of editing
MAX_DB_BYTES = 64 * 1024 * 1024
SQLITE_MAGIC = b"SQLite format 3\x00"

SQLJS_VERSION = "1.10.3"
SQLJS_FILES = ("sql-wasm.js", "sql-wasm.wasm", "sql-asm.js")
SQLJS_SOURCES = (
    f"https://cdnjs.cloudflare.com/ajax/libs/sql.js/{SQLJS_VERSION}/",
    f"https://cdn.jsdelivr.net/npm/sql.js@{SQLJS_VERSION}/dist/",
)

_db_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers
def etag_of(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


_etag_cache: tuple = (None, None)   # ((mtime_ns, size), etag)


def current_etag() -> str | None:
    """Version of the database on disk, without re-reading it unless it changed."""
    global _etag_cache
    try:
        st = DB_PATH.stat()
    except FileNotFoundError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if _etag_cache[0] != key:
        _etag_cache = (key, etag_of(DB_PATH.read_bytes()))
    return _etag_cache[1]


def page_version() -> str:
    """Changes whenever index.html changes (e.g. after updater.py installs an update)."""
    st = INDEX_HTML.stat()
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def norm_tag(tag: str) -> str:
    """Treat weak ETags (W/"...") the same as strong ones; reverse proxies sometimes add W/."""
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def current_db() -> bytes | None:
    try:
        return DB_PATH.read_bytes()
    except FileNotFoundError:
        return None


def make_backup(force: bool = False) -> None:
    """Copy the current database into backups/, rate-limited, keeping the newest KEEP_BACKUPS."""
    if not DB_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("tracker-*.db"))
    if existing and not force:
        last = dt.datetime.fromtimestamp(existing[-1].stat().st_mtime)
        if dt.datetime.now() - last < BACKUP_EVERY:
            return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DB_PATH, BACKUP_DIR / f"tracker-{stamp}.db")
    for old in sorted(BACKUP_DIR.glob("tracker-*.db"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def validate_sqlite(data: bytes) -> str | None:
    """Return an error message if `data` isn't a tracker database, else None."""
    if len(data) > MAX_DB_BYTES:
        return f"Database is larger than {MAX_DB_BYTES // (1024 * 1024)} MB."
    if not data.startswith(SQLITE_MAGIC):
        return "Upload is not a SQLite database."
    tmp = DATA_DIR / ".validate.tmp"
    try:
        tmp.write_bytes(data)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            ok = con.execute("PRAGMA quick_check").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        if ok != "ok":
            return f"Database failed its integrity check: {ok}"
        if tables and "clients" not in tables:
            return "This SQLite file isn't a tracker database."
    except sqlite3.DatabaseError as e:
        return f"Database could not be read: {e}"
    finally:
        tmp.unlink(missing_ok=True)
    return None


def write_db_atomic(data: bytes) -> None:
    tmp = DB_PATH.with_suffix(".db.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_PATH)


def fetch_sqljs(quiet: bool = False) -> None:
    """Download the SQLite engine once into data/vendor so the page works without a CDN."""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    stamp = VENDOR_DIR / "VERSION"
    if stamp.exists() and stamp.read_text().strip() == SQLJS_VERSION and all((VENDOR_DIR / f).exists() for f in SQLJS_FILES):
        return
    for base in SQLJS_SOURCES:
        try:
            for name in SQLJS_FILES:
                with urllib.request.urlopen(base + name, timeout=30) as r:
                    body = r.read()
                if name.endswith(".wasm") and not body.startswith(b"\x00asm"):
                    raise ValueError(f"{name} is not WebAssembly")
                (VENDOR_DIR / (name + ".part")).write_bytes(body)
            for name in SQLJS_FILES:
                os.replace(VENDOR_DIR / (name + ".part"), VENDOR_DIR / name)
            stamp.write_text(SQLJS_VERSION)
            if not quiet:
                print(f"  SQLite engine (sql.js {SQLJS_VERSION}) cached from {base}")
            return
        except Exception as e:  # noqa: BLE001 - try the next source
            if not quiet:
                print(f"  Couldn't download sql.js from {base}: {e}")
    if not quiet:
        print("  The page will load sql.js from a CDN in the browser instead.")


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def check_auth(environ) -> bool:
    if not PASSWORD:
        # No password configured: only allow this computer.
        return is_loopback(environ.get("REMOTE_ADDR", ""))
    header = environ.get("HTTP_AUTHORIZATION", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(user, USERNAME) & hmac.compare_digest(pw, PASSWORD)


# --------------------------------------------------------------------------- WSGI app
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
]
MIME = {".js": "text/javascript; charset=utf-8", ".wasm": "application/wasm", ".html": "text/html; charset=utf-8"}


_gz_cache: dict = {}


def gzipped(data: bytes, tag: str) -> bytes:
    """Database files shrink a lot when compressed; keep the last one so repeated loads don't redo it."""
    hit = _gz_cache.get(tag)              # safe with several requests at once
    if hit is None:
        hit = gzip.compress(data, compresslevel=6)
        _gz_cache.clear()
        _gz_cache[tag] = hit
    return hit


def gunzip_limited(data: bytes, limit: int) -> bytes | None:
    """Decompress a gzip upload, refusing anything that would expand past `limit` (zip bombs)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or d.unconsumed_tail or not d.eof:
        return None
    return out


def respond(start_response, status: str, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", headers=None):
    hdrs = [("Content-Type", ctype), ("Content-Length", str(len(body)))] + SECURITY_HEADERS + (headers or [])
    start_response(status, hdrs)
    return [body]


def json_response(start_response, status: str, obj, headers=None):
    return respond(start_response, status, json.dumps(obj).encode(), "application/json", [("Cache-Control", "no-store")] + (headers or []))


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"

    if path == "/healthz":
        return respond(start_response, "200 OK", b"ok")

    if not check_auth(environ):
        if not PASSWORD:
            return respond(start_response, "403 Forbidden",
                           b"This tracker only accepts connections from this computer.\n"
                           b"Set TRACKER_PASSWORD on the server to allow other devices.\n")
        time.sleep(1)  # slow down password guessing
        return respond(start_response, "401 Unauthorized", b"Password required.\n",
                       headers=[("WWW-Authenticate", 'Basic realm="Bookkeeping Tracker", charset="UTF-8"')])

    # --- 3dfun: a separate just-for-fun 3D page. To remove it, delete this block and the fun/ folder.
    if (path.rstrip("/") == "/3dfun" or (path == "/" and environ.get("QUERY_STRING") == "3dfun")) and method in ("GET", "HEAD"):
        fun = ROOT / "fun" / "3dfun.html"
        if fun.exists():
            body = fun.read_bytes()
            return respond(start_response, "200 OK", b"" if method == "HEAD" else body, MIME[".html"], [("Cache-Control", "no-cache")])
    # --- end 3dfun

    # --- page
    if path in ("/", "/index.html") and method in ("GET", "HEAD"):
        body = INDEX_HTML.read_bytes()
        return respond(start_response, "200 OK", b"" if method == "HEAD" else body, MIME[".html"], [("Cache-Control", "no-cache")])

    # --- SQLite engine
    if path.startswith("/vendor/") and method in ("GET", "HEAD"):
        name = path[len("/vendor/"):]
        f = VENDOR_DIR / name
        if name not in SQLJS_FILES or not f.exists():
            return respond(start_response, "404 Not Found", b"not cached")
        body = f.read_bytes()
        return respond(start_response, "200 OK", b"" if method == "HEAD" else body,
                       MIME.get(f.suffix, "application/octet-stream"), [("Cache-Control", "public, max-age=604800, immutable")])

    # --- API
    if path == "/api/info" and method == "GET":
        tag = current_etag()
        return json_response(start_response, "200 OK", {
            "server": True, "exists": tag is not None, "etag": tag,
            "size": DB_PATH.stat().st_size if tag else 0, "protected": bool(PASSWORD), "page": page_version(),
        })

    if path == "/api/db" and method in ("GET", "HEAD"):
        with _db_lock:
            data = current_db()
        if data is None:
            return json_response(start_response, "404 Not Found", {"error": "No database on the server yet."})
        tag = etag_of(data)
        headers = [("ETag", tag), ("Cache-Control", "no-store"), ("Vary", "Accept-Encoding")]
        if "download" in environ.get("QUERY_STRING", ""):
            headers.append(("Content-Disposition", f'attachment; filename="tracker_{dt.date.today()}.db"'))
        elif method == "GET" and "gzip" in environ.get("HTTP_ACCEPT_ENCODING", "") and "plain" not in environ.get("QUERY_STRING", ""):
            headers.append(("Content-Encoding", "gzip"))   # the browser unpacks this by itself
            data = gzipped(data, tag)
        return respond(start_response, "200 OK", b"" if method == "HEAD" else data, "application/x-sqlite3", headers)

    if path == "/api/db" and method == "PUT":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_DB_BYTES:
            return json_response(start_response, "413 Payload Too Large", {"error": "Missing or oversized database upload."})
        data = environ["wsgi.input"].read(length)
        if len(data) != length:
            return json_response(start_response, "400 Bad Request", {"error": "Upload was cut off."})
        enc = environ.get("HTTP_CONTENT_ENCODING", "").strip().lower()
        if enc == "gzip":
            data = gunzip_limited(data, MAX_DB_BYTES)
            if data is None:
                return json_response(start_response, "413 Payload Too Large", {"error": "Compressed upload is damaged or too large."})
        elif enc not in ("", "identity"):
            return json_response(start_response, "415 Unsupported Media Type", {"error": f"Unsupported Content-Encoding: {enc}"})
        if err := validate_sqlite(data):
            return json_response(start_response, "422 Unprocessable Entity", {"error": err})
        if_match = norm_tag(environ.get("HTTP_IF_MATCH", ""))
        with _db_lock:
            existing = current_db()
            if existing is not None:
                cur = etag_of(existing)
                if not if_match:
                    return json_response(start_response, "428 Precondition Required",
                                         {"error": "Send If-Match with the version you loaded.", "etag": cur})
                if if_match != "*" and if_match != cur:
                    return json_response(start_response, "409 Conflict",
                                         {"error": "The database was changed from another window or device.", "etag": cur})
                if cur == etag_of(data):
                    return json_response(start_response, "200 OK", {"etag": cur, "unchanged": True}, [("ETag", cur)])
                make_backup(force=(if_match == "*"))   # always back up before a forced replace
            write_db_atomic(data)
            new = etag_of(data)
        return json_response(start_response, "200 OK", {"etag": new}, [("ETag", new)])

    if path == "/api/backups" and method == "GET":
        files = sorted(BACKUP_DIR.glob("tracker-*.db"), reverse=True) if BACKUP_DIR.exists() else []
        return json_response(start_response, "200 OK", [{"name": f.name, "size": f.stat().st_size} for f in files])

    return respond(start_response, "404 Not Found", b"Not found")


# --------------------------------------------------------------------------- startup
def prepare(quiet: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_HTML.exists():
        sys.exit(f"index.html not found next to {__file__}")
    threading.Thread(target=fetch_sqljs, kwargs={"quiet": quiet}, daemon=True).start()


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        if str(code) not in ("200", "304"):
            super().log_request(code, size)


def main() -> None:
    p = argparse.ArgumentParser(description="Bookkeeping Tracker server")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--import-db", metavar="FILE", help="copy an existing .db into the server's data folder and exit")
    args = p.parse_args()

    if args.import_db:
        data = Path(args.import_db).read_bytes()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if err := validate_sqlite(data):
            sys.exit(err)
        make_backup(force=True)
        write_db_atomic(data)
        print(f"Imported {args.import_db} -> {DB_PATH}")
        return

    if not PASSWORD and not is_loopback(args.host) and args.host != "localhost":
        print("WARNING: no TRACKER_PASSWORD set, so only this computer can connect (other devices get 403).")

    print(f"Bookkeeping Tracker\n  data folder: {DATA_DIR}")
    prepare()
    with make_server(args.host, args.port, application, server_class=ThreadingWSGIServer, handler_class=QuietHandler) as httpd:
        shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
        print(f"  open http://{shown}:{args.port}/   (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
else:
    prepare(quiet=True)  # imported by a WSGI server (PythonAnywhere, gunicorn, waitress…)

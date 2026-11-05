"""End-to-end tests for app.py. Standard library only: python -m unittest discover tests"""
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTH = "tracker:test-password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tracker_db(path: Path, rows: int = 1) -> bytes:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE clients(id INTEGER PRIMARY KEY, name TEXT)")
    con.executemany("INSERT INTO clients(name) VALUES (?)", [(f"client {i}",) for i in range(rows)])
    con.commit()
    con.close()
    return path.read_bytes()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.port = free_port()
        env = dict(os.environ, TRACKER_DATA_DIR=str(cls.dir / "data"), TRACKER_PASSWORD="test-password")
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--port", str(cls.port)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.tmp.cleanup()

    def req(self, method, path, data=None, headers=None, auth=AUTH):
        h = dict(headers or {})
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_01_auth(self):
        self.assertEqual(self.req("GET", "/", auth=None)[0], 401)
        self.assertEqual(self.req("GET", "/", auth="tracker:wrong")[0], 401)
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Reconciliation Tracker", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_02_save_load_and_conflicts(self):
        self.assertEqual(self.req("GET", "/api/db")[0], 404)
        first = tracker_db(self.dir / "a.db", 1)
        status, _, body = self.req("PUT", "/api/db", first, {"If-Match": "*"})
        self.assertEqual(status, 200)
        tag1 = json.loads(body)["etag"]

        status, headers, body = self.req("GET", "/api/db")
        self.assertEqual((status, headers["ETag"], body), (200, tag1, first))

        second = tracker_db(self.dir / "a.db", 2)
        self.assertEqual(self.req("PUT", "/api/db", second)[0], 428)                          # no If-Match
        self.assertEqual(self.req("PUT", "/api/db", second, {"If-Match": '"stale"'})[0], 409)  # wrong version
        status, _, body = self.req("PUT", "/api/db", second, {"If-Match": tag1})
        self.assertEqual(status, 200)
        self.assertNotEqual(json.loads(body)["etag"], tag1)
        # A second tab still holding the first version must not overwrite.
        self.assertEqual(self.req("PUT", "/api/db", tracker_db(self.dir / "a.db", 3), {"If-Match": tag1})[0], 409)

    def test_03_rejects_bad_uploads(self):
        self.assertEqual(self.req("PUT", "/api/db", b"not a database", {"If-Match": "*"})[0], 422)
        other = self.dir / "other.db"
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE something_else(x)")
        con.commit()
        con.close()
        self.assertEqual(self.req("PUT", "/api/db", other.read_bytes(), {"If-Match": "*"})[0], 422)

    def test_04_backup_on_forced_replace(self):
        self.req("PUT", "/api/db", tracker_db(self.dir / "a.db", 1), {"If-Match": "*"})
        self.req("PUT", "/api/db", tracker_db(self.dir / "a.db", 5), {"If-Match": "*"})
        backups = json.loads(self.req("GET", "/api/backups")[2])
        self.assertGreaterEqual(len(backups), 1)

    def test_05_info_tracks_version_and_weak_etags(self):
        status, _, body = self.req("PUT", "/api/db", tracker_db(self.dir / "a.db", 7), {"If-Match": "*"})
        tag = json.loads(body)["etag"]
        info = json.loads(self.req("GET", "/api/info")[2])
        self.assertEqual(info["etag"], tag)
        self.assertTrue(info["page"])
        # A proxy may hand the page a weak ETag (W/"..."); saving with it must still work.
        status, _, body = self.req("PUT", "/api/db", tracker_db(self.dir / "a.db", 8), {"If-Match": "W/" + tag})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.req("GET", "/api/info")[2])["etag"], json.loads(body)["etag"])

    def test_07_gzip_both_ways(self):
        import gzip
        raw = tracker_db(self.dir / "a.db", 300)
        status, _, body = self.req("PUT", "/api/db", gzip.compress(raw), {"If-Match": "*", "Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        tag = json.loads(body)["etag"]
        status, headers, body = self.req("GET", "/api/db", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((status, headers["Content-Encoding"], headers["ETag"]), (200, "gzip", tag))
        self.assertLess(len(body), len(raw))
        self.assertEqual(gzip.decompress(body), raw)
        self.assertEqual(self.req("GET", "/api/db")[2], raw)                        # plain for clients that don't ask
        bomb = gzip.compress(b"\0" * (65 * 1024 * 1024))                            # expands past the 64 MB limit
        self.assertEqual(self.req("PUT", "/api/db", bomb, {"If-Match": "*", "Content-Encoding": "gzip"})[0], 413)
        self.assertEqual(self.req("PUT", "/api/db", b"junk", {"If-Match": "*", "Content-Encoding": "gzip"})[0], 413)
        self.assertEqual(self.req("PUT", "/api/db", raw, {"If-Match": "*", "Content-Encoding": "br"})[0], 415)

    def test_06_vendor_paths_are_restricted(self):
        self.assertEqual(self.req("GET", "/vendor/../tracker.db")[0], 404)
        self.assertEqual(self.req("GET", "/vendor/anything.js")[0], 404)


if __name__ == "__main__":
    unittest.main()

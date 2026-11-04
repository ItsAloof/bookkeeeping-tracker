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

    def test_05_vendor_paths_are_restricted(self):
        self.assertEqual(self.req("GET", "/vendor/../tracker.db")[0], 404)
        self.assertEqual(self.req("GET", "/vendor/anything.js")[0], 404)


if __name__ == "__main__":
    unittest.main()

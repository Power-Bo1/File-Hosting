"""End-to-end tests for the API endpoints using a fake in-memory DB.

Needs: fastapi, httpx, bcrypt, psycopg2, PyJWT
    pip install -r api/requirements.txt -r requirements-dev.txt
Skipped automatically when those aren't available.
"""
import datetime
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest

HAVE_DEPS = all(importlib.util.find_spec(m)
                for m in ("fastapi", "httpx", "bcrypt", "psycopg2", "jwt"))


@unittest.skipUnless(HAVE_DEPS, "install api/requirements.txt + requirements-dev.txt to run")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["JWT_SECRET"] = "test-secret-long-enough-for-hmac-sha256!!"
        os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="apitest-")
        os.environ["MAX_UPLOAD_MB"] = "1"          # small cap to test 413
        os.environ["APP_BASE_URL"] = "https://files.local"
        os.environ.setdefault("DB_HOST", "unused")
        os.environ.setdefault("DB_NAME", "unused")
        os.environ.setdefault("DB_USER", "unused")
        os.environ.setdefault("DB_PASSWORD", "unused")
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
        import app as apimod
        import psycopg2
        from fastapi.testclient import TestClient

        cls.apimod = apimod
        cls.state = {"users": {}, "files": {}, "resets": {}, "revoked": {},
                     "next_id": 1, "next_pr": 1}
        cls.sent = []          # captured outbound emails (to, subject, body)
        apimod.send_email = lambda to, subject, body: cls.sent.append(
            {"to": to, "subject": subject, "body": body})

        class FakeCursor:
            def __init__(self, st):
                self.st, self._rows = st, []

            def execute(self, sql, params=None):
                s, st = " ".join(sql.split()), self.st
                if s.startswith("INSERT INTO users"):
                    u, h, e = params
                    if u in st["users"] or any(
                            v["email"] == e for v in st["users"].values()):
                        raise psycopg2.errors.UniqueViolation()
                    uid = st["next_id"]; st["next_id"] += 1
                    st["users"][u] = {"id": uid, "hash": h, "email": e, "ver": 0}
                    self._rows = [(uid,)]
                elif s.startswith("SELECT id, password_hash, token_version"):
                    v = st["users"].get(params[0])
                    self._rows = [(v["id"], v["hash"], v["ver"])] if v else []
                elif s.startswith("SELECT u.token_version"):
                    jti, uid = params
                    hit = [v for v in st["users"].values() if v["id"] == uid]
                    self._rows = [(hit[0]["ver"], jti in st["revoked"])] if hit else []
                elif s.startswith("DELETE FROM revoked_tokens WHERE expires_at"):
                    pass
                elif s.startswith("INSERT INTO revoked_tokens"):
                    jti, uid, exp = params
                    st["revoked"][jti] = (uid, exp)
                elif s.startswith("SELECT id, username FROM users WHERE email"):
                    hits = [(v["id"], u) for u, v in st["users"].items()
                            if v["email"] == params[0]]
                    self._rows = hits[:1]
                elif s.startswith("DELETE FROM password_resets WHERE expires_at"):
                    now = params[0]
                    st["resets"] = {k: v for k, v in st["resets"].items()
                                    if v["expires_at"] >= now}
                elif s.startswith("DELETE FROM password_resets WHERE user_id"):
                    uid = params[0]
                    st["resets"] = {k: v for k, v in st["resets"].items()
                                    if v["user_id"] != uid}
                elif s.startswith("INSERT INTO password_resets"):
                    uid, thash, exp = params
                    prid = st["next_pr"]; st["next_pr"] += 1
                    st["resets"][thash] = {"id": prid, "user_id": uid,
                                           "expires_at": exp, "used_at": None}
                elif s.startswith("SELECT pr.id, pr.user_id"):
                    v = st["resets"].get(params[0])
                    if not v:
                        self._rows = []
                    else:
                        uname, email = next(
                            (u, d["email"]) for u, d in st["users"].items()
                            if d["id"] == v["user_id"])
                        self._rows = [(v["id"], v["user_id"], v["expires_at"],
                                       v["used_at"], uname, email)]
                elif s.startswith("UPDATE users SET password_hash"):
                    new_hash, uid = params
                    for d in st["users"].values():
                        if d["id"] == uid:
                            d["hash"] = new_hash
                            d["ver"] += 1
                elif s.startswith("UPDATE password_resets SET used_at"):
                    used_at, prid = params
                    for v in st["resets"].values():
                        if v["id"] == prid:
                            v["used_at"] = used_at
                elif s.startswith("INSERT INTO files"):
                    client, fname, size, uid = params
                    st["files"][(uid, fname)] = (client, size)
                elif s.startswith("SELECT size FROM files WHERE user_id"):
                    uid, fname = params
                    hit = st["files"].get((uid, fname))
                    self._rows = [(hit[1],)] if hit else []
                elif s.startswith("DELETE FROM files WHERE user_id"):
                    uid, fname = params
                    self._rows = [(1,)] if st["files"].pop((uid, fname), None) else []
                elif s.startswith("SELECT filename, size"):
                    uid = params[0]
                    self._rows = [(f, sz, "2026-01-01")
                                  for (u, f), (_, sz) in st["files"].items()
                                  if u == uid]
                elif s.startswith("SELECT 1"):
                    self._rows = [(1,)]
                else:   # CREATE / ALTER / DELETE migrations -> no-op
                    self._rows = []

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

            def close(self):
                pass

        class FakeConn:
            def __init__(self, st):
                self.st = st

            def cursor(self):
                return FakeCursor(self.st)

            def commit(self): pass
            def rollback(self): pass
            def close(self): pass

        cls._real_db = apimod.db          # keep the real one for a regression test
        apimod.db = lambda: FakeConn(cls.state)
        cls.client = TestClient(apimod.app)
        cls.client.__enter__()                     # runs lifespan/init_db

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    # ---- helpers -------------------------------------------------------
    def register(self, u, p, email=None):
        return self.client.post("/auth/register", json={
            "username": u, "password": p,
            "email": email if email is not None else f"{u}@test.local"})

    def token(self, u="alice", p="longenough"):
        self.register(u, p)
        r = self.client.post("/auth/login", json={"username": u, "password": p})
        return r.json()["access_token"]

    def auth(self, tok):
        return {"Authorization": f"Bearer {tok}"}

    def reset_token_from_email(self, email):
        body = [m["body"] for m in self.sent if m["to"] == email][-1]
        return re.search(r"token=([A-Za-z0-9_\-]+)", body).group(1)

    # ---- auth ----------------------------------------------------------
    def test_register_login_roundtrip(self):
        self.assertEqual(self.register("bob", "longenough").status_code, 201)
        r = self.client.post("/auth/login",
                             json={"username": "bob", "password": "longenough"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("access_token", r.json())

    def test_register_duplicate_409(self):
        self.register("carol", "longenough")
        self.assertEqual(self.register("carol", "longenough").status_code, 409)

    def test_register_duplicate_email_409(self):
        self.register("dup1", "longenough", email="same@test.local")
        r = self.register("dup2", "longenough", email="same@test.local")
        self.assertEqual(r.status_code, 409)

    def test_register_requires_valid_email(self):
        r = self.register("noemail", "longenough", email="not-an-email")
        self.assertEqual(r.status_code, 400)

    def test_register_short_password_400(self):
        self.assertEqual(self.register("dave", "short").status_code, 400)

    def test_login_wrong_password_401(self):
        self.register("erin", "longenough")
        r = self.client.post("/auth/login",
                             json={"username": "erin", "password": "wrongpass1"})
        self.assertEqual(r.status_code, 401)

    def test_login_unknown_user_401_same_message(self):
        r = self.client.post("/auth/login",
                             json={"username": "ghost", "password": "longenough"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"], "Invalid username or password")

    # ---- protected routes ----------------------------------------------
    def test_files_requires_token(self):
        # Missing creds: 401 (current FastAPI) or 403 (older versions)
        self.assertIn(self.client.get("/files").status_code, (401, 403))
        r = self.client.get("/files", headers=self.auth("garbage"))
        self.assertEqual(r.status_code, 401)

    def test_upload_and_list_isolated_per_user(self):
        t1, t2 = self.token("frank"), self.token("grace")
        r = self.client.post("/upload", headers=self.auth(t1),
                             files={"file": ("a.txt", b"hello", "text/plain")})
        self.assertEqual(r.status_code, 200)
        names1 = [f["filename"] for f in
                  self.client.get("/files", headers=self.auth(t1)).json()]
        names2 = [f["filename"] for f in
                  self.client.get("/files", headers=self.auth(t2)).json()]
        self.assertIn("a.txt", names1)
        self.assertNotIn("a.txt", names2)

    def test_upload_oversize_413_and_no_leftover_part(self):
        t = self.token("henry")
        big = io.BytesIO(b"x" * (2 * 1024 * 1024))   # 2 MB vs 1 MB cap
        r = self.client.post("/upload", headers=self.auth(t),
                             files={"file": ("big.bin", big, "application/octet-stream")})
        self.assertEqual(r.status_code, 413)
        leftovers = [p for _, _, fs in os.walk(os.environ["DATA_DIR"])
                     for p in fs if p.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_upload_bad_filename_400(self):
        t = self.token("iris")
        r = self.client.post("/upload", headers=self.auth(t),
                             files={"file": ("..", b"data", "text/plain")})
        self.assertEqual(r.status_code, 400)

    def test_health_and_ready(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/ready").status_code, 200)

    # ---- v2.1.1 regression: the /ready hang -----------------------------
    def test_ready_returns_within_budget(self):
        """Guards against /ready blocking (the CrashLoopBackOff bug)."""
        import time
        start = time.time()
        r = self.client.get("/ready")
        elapsed = time.time() - start
        self.assertEqual(r.status_code, 200)
        self.assertLess(elapsed, 2.0, f"/ready took {elapsed:.2f}s (should be ~instant)")

    def test_ready_returns_503_when_db_down(self):
        """DB unreachable -> 503, not a hang or a 500."""
        def boom():
            raise Exception("simulated db outage")
        orig = self.apimod.db
        self.apimod.db = boom
        try:
            r = self.client.get("/ready")
        finally:
            self.apimod.db = orig
        self.assertEqual(r.status_code, 503)

    def test_db_passes_connect_timeout(self):
        """The real db() must hand psycopg2 a connect_timeout, so a
        connection attempt can never hang forever (root cause of the bug)."""
        import psycopg2
        captured = {}
        orig_connect = psycopg2.connect

        def fake_connect(**kwargs):
            captured.update(kwargs)
            raise psycopg2.OperationalError("stop before real connect")

        psycopg2.connect = fake_connect
        try:
            with self.assertRaises(psycopg2.OperationalError):
                self.__class__._real_db()
        finally:
            psycopg2.connect = orig_connect
        self.assertIn("connect_timeout", captured)
        self.assertGreater(int(captured["connect_timeout"]), 0)

    # ---- v2.3: password reset flow --------------------------------------
    def test_forgot_unknown_email_generic_and_silent(self):
        before = len(self.sent)
        r = self.client.post("/auth/forgot", json={"email": "nobody@test.local"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("If that email is registered", r.json()["message"])
        self.assertEqual(len(self.sent), before)     # no email leaked

    def test_forgot_known_email_sends_hashed_single_token(self):
        self.register("judy", "longenough")
        r = self.client.post("/auth/forgot", json={"email": "judy@test.local"})
        self.assertEqual(r.status_code, 200)
        token = self.reset_token_from_email("judy@test.local")
        self.assertGreaterEqual(len(token), 40)
        # Only the hash is stored - the raw token appears nowhere in state
        self.assertNotIn(token, self.state["resets"])
        from security import hash_token
        self.assertIn(hash_token(token), self.state["resets"])

    def test_reset_garbage_token_400(self):
        r = self.client.post("/auth/reset", json={
            "token": "garbage", "new_password": "newlongenough"})
        self.assertEqual(r.status_code, 400)

    def test_reset_expired_token_400(self):
        self.register("kate", "longenough")
        self.client.post("/auth/forgot", json={"email": "kate@test.local"})
        token = self.reset_token_from_email("kate@test.local")
        from security import hash_token
        self.state["resets"][hash_token(token)]["expires_at"] = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=1))
        r = self.client.post("/auth/reset", json={
            "token": token, "new_password": "newlongenough"})
        self.assertEqual(r.status_code, 400)

    # ---- v2.9: download + delete ---------------------------------------
    def upload_as(self, tok, name, content=b"payload"):
        return self.client.post("/upload", headers=self.auth(tok),
                                files={"file": (name, content, "text/plain")})

    def test_download_own_file_returns_content(self):
        t = self.token("rita")
        self.upload_as(t, "note.txt", b"hello rita")
        r = self.client.get("/files/note.txt", headers=self.auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"hello rita")

    def test_download_is_attachment_octet_stream(self):
        """An uploaded .html must never render in the app's origin."""
        t = self.token("sam")
        self.upload_as(t, "evil.html", b"<script>alert(1)</script>")
        r = self.client.get("/files/evil.html", headers=self.auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        self.assertIn("attachment", r.headers["content-disposition"])

    def test_cannot_download_another_users_file(self):
        t1, t2 = self.token("tara"), self.token("uri")
        self.upload_as(t1, "secret.txt", b"tara only")
        r = self.client.get("/files/secret.txt", headers=self.auth(t2))
        self.assertEqual(r.status_code, 404)   # 404 not 403: no existence leak

    def test_download_missing_file_404(self):
        t = self.token("vic")
        self.assertEqual(
            self.client.get("/files/nope.txt", headers=self.auth(t)).status_code, 404)

    def test_download_requires_auth(self):
        self.assertIn(self.client.get("/files/x.txt").status_code, (401, 403))

    def test_download_path_traversal_blocked(self):
        t = self.token("wes")
        for evil in ("..", "%2e%2e%2fetc%2fpasswd", "....//etc//passwd"):
            r = self.client.get(f"/files/{evil}", headers=self.auth(t))
            self.assertIn(r.status_code, (400, 404),
                          f"{evil} returned {r.status_code}")

    def test_delete_removes_row_and_blob(self):
        t = self.token("xena")
        self.upload_as(t, "gone.txt", b"bye")
        import os
        uid = self.state["users"]["xena"]["id"]
        base_dir = os.path.realpath(os.environ["DATA_DIR"])
        path = os.path.realpath(os.path.join(base_dir, str(uid), "gone.txt"))
        self.assertEqual(os.path.commonpath([base_dir, path]), base_dir)
        self.assertTrue(os.path.isfile(path))

        r = self.client.delete("/files/gone.txt", headers=self.auth(t))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.isfile(path))                       # blob gone
        names = [f["filename"] for f in
                 self.client.get("/files", headers=self.auth(t)).json()]
        self.assertNotIn("gone.txt", names)                          # row gone
        self.assertEqual(
            self.client.get("/files/gone.txt", headers=self.auth(t)).status_code, 404)

    def test_cannot_delete_another_users_file(self):
        t1, t2 = self.token("yara"), self.token("zack")
        self.upload_as(t1, "mine.txt", b"keep")
        self.assertEqual(
            self.client.delete("/files/mine.txt", headers=self.auth(t2)).status_code, 404)
        names = [f["filename"] for f in
                 self.client.get("/files", headers=self.auth(t1)).json()]
        self.assertIn("mine.txt", names)      # untouched

    def test_delete_missing_file_404(self):
        t = self.token("amy")
        self.assertEqual(
            self.client.delete("/files/nope.txt", headers=self.auth(t)).status_code, 404)

    def test_delete_with_revoked_token_401(self):
        t = self.token("ben")
        self.upload_as(t, "keep.txt", b"x")
        self.client.post("/auth/logout", headers=self.auth(t))
        self.assertEqual(
            self.client.delete("/files/keep.txt", headers=self.auth(t)).status_code, 401)

    # ---- v2.8: per-token denylist (logout revokes ONE session) ---------
    def test_logout_revokes_only_that_session(self):
        """The whole point of jti: other devices stay logged in."""
        self.register("nora", "longenough")
        a = self.client.post("/auth/login",
                             json={"username": "nora", "password": "longenough"}
                             ).json()["access_token"]
        b = self.client.post("/auth/login",
                             json={"username": "nora", "password": "longenough"}
                             ).json()["access_token"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.client.get("/files", headers=self.auth(a)).status_code, 200)
        self.assertEqual(self.client.get("/files", headers=self.auth(b)).status_code, 200)

        self.assertEqual(
            self.client.post("/auth/logout", headers=self.auth(a)).status_code, 200)

        r = self.client.get("/files", headers=self.auth(a))
        self.assertEqual(r.status_code, 401)
        self.assertIn("logged out", r.json()["detail"].lower())
        # the OTHER device is untouched
        self.assertEqual(self.client.get("/files", headers=self.auth(b)).status_code, 200)

    def test_logout_is_idempotent(self):
        t = self.token("oscar")
        self.assertEqual(self.client.post("/auth/logout", headers=self.auth(t)).status_code, 200)
        self.assertEqual(self.client.post("/auth/logout", headers=self.auth(t)).status_code, 200)

    def test_revoked_token_cannot_upload(self):
        t = self.token("pia")
        self.client.post("/auth/logout", headers=self.auth(t))
        r = self.client.post("/upload", headers=self.auth(t),
                             files={"file": ("x.txt", b"data", "text/plain")})
        self.assertEqual(r.status_code, 401)

    def test_logout_rejects_garbage_token(self):
        self.assertEqual(
            self.client.post("/auth/logout", headers=self.auth("garbage")).status_code, 401)

    def test_denylist_stores_the_jti_not_the_token(self):
        """A leaked denylist row must not be a usable credential."""
        t = self.token("quinn")
        self.client.post("/auth/logout", headers=self.auth(t))
        self.assertTrue(self.state["revoked"])
        for jti in self.state["revoked"]:
            self.assertNotIn(jti, t)          # jti is not a slice of the JWT
            self.assertLess(len(jti), len(t))

    def test_reset_happy_path_kills_old_sessions_single_use(self):
        old_token = self.token("liam")                     # login pre-reset
        self.client.post("/auth/forgot", json={"email": "liam@test.local"})
        reset_tok = self.reset_token_from_email("liam@test.local")

        r = self.client.post("/auth/reset", json={
            "token": reset_tok, "new_password": "brandnewpass1"})
        self.assertEqual(r.status_code, 200)

        # 1. every pre-reset session is dead (token_version bumped)
        r = self.client.get("/files", headers=self.auth(old_token))
        self.assertEqual(r.status_code, 401)
        # 2. old password no longer works
        r = self.client.post("/auth/login",
                             json={"username": "liam", "password": "longenough"})
        self.assertEqual(r.status_code, 401)
        # 3. new password works, and the fresh token is accepted
        r = self.client.post("/auth/login",
                             json={"username": "liam", "password": "brandnewpass1"})
        self.assertEqual(r.status_code, 200)
        fresh = r.json()["access_token"]
        self.assertEqual(
            self.client.get("/files", headers=self.auth(fresh)).status_code, 200)
        # 4. confirmation email went out
        subjects = [m["subject"] for m in self.sent if m["to"] == "liam@test.local"]
        self.assertTrue(any("changed" in s for s in subjects))
        # 5. the link is single-use
        r = self.client.post("/auth/reset", json={
            "token": reset_tok, "new_password": "anotherpass1"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

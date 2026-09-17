"""Unit tests for api/security.py - pure logic, runs anywhere PyJWT exists."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
from security import (  # noqa: E402
    make_token, decode_token, sanitize_filename, validate_username,
    validate_password, validate_email, new_reset_token, hash_token,
    TokenExpired, TokenInvalid,
)

SECRET = "test-secret-long-enough-for-hmac-sha256!!"


class TokenTests(unittest.TestCase):
    def test_roundtrip_with_version(self):
        tok = make_token(SECRET, 42, "alice", 60, ver=3)
        d = decode_token(SECRET, tok)
        self.assertEqual(d["id"], 42)
        self.assertEqual(d["username"], "alice")
        self.assertEqual(d["ver"], 3)

    def test_every_token_gets_a_unique_jti(self):
        a = decode_token(SECRET, make_token(SECRET, 1, "alice", 60))
        b = decode_token(SECRET, make_token(SECRET, 1, "alice", 60))
        self.assertTrue(a["jti"] and b["jti"])
        self.assertNotEqual(a["jti"], b["jti"])

    def test_decode_exposes_exp_for_denylist_pruning(self):
        d = decode_token(SECRET, make_token(SECRET, 1, "alice", 60))
        self.assertIsInstance(d["exp"], int)

    def test_legacy_token_without_jti_still_decodes(self):
        import jwt as _jwt, datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        legacy = _jwt.encode({"sub": "5", "username": "old", "ver": 0, "iat": now,
                              "exp": now + _dt.timedelta(minutes=5)},
                             SECRET, algorithm="HS256")
        d = decode_token(SECRET, legacy)
        self.assertEqual(d["id"], 5)
        self.assertEqual(d["jti"], "")   # cannot be revoked, expires on its own

    def test_ver_defaults_to_zero(self):
        tok = make_token(SECRET, 1, "alice", 60)
        self.assertEqual(decode_token(SECRET, tok)["ver"], 0)

    def test_wrong_secret_rejected(self):
        tok = make_token(SECRET, 1, "alice", 60)
        with self.assertRaises(TokenInvalid):
            decode_token("other-secret-that-is-also-long-enough", tok)

    def test_garbage_rejected(self):
        with self.assertRaises(TokenInvalid):
            decode_token(SECRET, "not.a.token")

    def test_expired_rejected(self):
        tok = make_token(SECRET, 1, "alice", 0)   # expires immediately
        time.sleep(1.1)
        with self.assertRaises(TokenExpired):
            decode_token(SECRET, tok)

    def test_tampered_payload_rejected(self):
        tok = make_token(SECRET, 1, "alice", 60)
        h, p, s = tok.split(".")
        with self.assertRaises(TokenInvalid):
            decode_token(SECRET, f"{h}.{p}x.{s}")


class ResetTokenTests(unittest.TestCase):
    def test_reset_token_pair(self):
        token, thash = new_reset_token()
        self.assertGreaterEqual(len(token), 40)      # 32 bytes urlsafe
        self.assertEqual(len(thash), 64)             # sha256 hex
        self.assertEqual(hash_token(token), thash)

    def test_reset_tokens_unique(self):
        self.assertNotEqual(new_reset_token()[0], new_reset_token()[0])


class FilenameTests(unittest.TestCase):
    def test_plain_name_ok(self):
        self.assertEqual(sanitize_filename("report.pdf"), "report.pdf")

    def test_path_traversal_stripped(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("/etc/shadow"), "shadow")

    def test_dotdot_rejected(self):
        for bad in ("", None, ".", ".."):
            with self.assertRaises(ValueError):
                sanitize_filename(bad)

    def test_nul_bytes_stripped(self):
        self.assertEqual(sanitize_filename("a\x00b.txt"), "ab.txt")

    def test_overlong_rejected(self):
        with self.assertRaises(ValueError):
            sanitize_filename("x" * 300)


class CredentialRuleTests(unittest.TestCase):
    def test_username_bounds(self):
        self.assertEqual(validate_username("  bob "), "bob")
        for bad in ("ab", "x" * 65, "", None):
            with self.assertRaises(ValueError):
                validate_username(bad)

    def test_password_bounds(self):
        validate_password("longenough")
        with self.assertRaises(ValueError):
            validate_password("short")
        with self.assertRaises(ValueError):
            validate_password("x" * 73)
        with self.assertRaises(ValueError):
            validate_password("é" * 40)   # 80 bytes

    def test_valid_emails_normalized(self):
        self.assertEqual(validate_email("  Bob@Example.COM "), "bob@example.com")

    def test_invalid_emails_rejected(self):
        for bad in ("", None, "plain", "a@b", "a b@c.com", "a@b .com",
                    "x" * 250 + "@e.com"):
            with self.assertRaises(ValueError):
                validate_email(bad)


if __name__ == "__main__":
    unittest.main()


class ContentSecurityPolicyTests(unittest.TestCase):
    """The CSP is the enforcement layer behind the CSS rules: without it,
    'no url() in our stylesheet' is a convention an injected <style>
    ignores. Assert the shipped header keeps its teeth."""

    @classmethod
    def setUpClass(cls):
        import glob
        import yaml
        path = os.path.join(os.path.dirname(__file__), "..",
                            "k8s", "ingress-nginx-headers.yaml")
        if not os.path.isfile(path):
            raise unittest.SkipTest("headers manifest not present")
        doc = next(d for d in yaml.safe_load_all(open(path)) if d)
        cls.csp = " ".join(doc["data"]["Content-Security-Policy"].split())

    def test_style_src_is_self_without_unsafe_inline(self):
        self.assertIn("style-src 'self'", self.csp)
        self.assertNotIn("unsafe-inline", self.csp)

    def test_no_unsafe_eval(self):
        self.assertNotIn("unsafe-eval", self.csp)

    def test_scripts_are_blocked_entirely(self):
        self.assertIn("script-src 'none'", self.csp)

    def test_font_and_connect_channels_closed(self):
        self.assertIn("font-src 'none'", self.csp)
        self.assertIn("connect-src 'none'", self.csp)

    def test_framing_and_base_uri_locked(self):
        self.assertIn("frame-ancestors 'none'", self.csp)
        self.assertIn("base-uri 'self'", self.csp)

    def test_forms_cannot_post_offsite(self):
        self.assertIn("form-action 'self'", self.csp)


class DeceptiveInputTests(unittest.TestCase):
    """Characters whose only purpose is to make one thing look like
    another. Filenames strip them (a legitimate name may contain accents
    or CJK); usernames reject anything outside a safe ASCII set."""

    def test_rtl_override_is_stripped_from_filename(self):
        # "invoice<RLO>gnp.exe" renders to a human as "invoiceexe.png"
        self.assertEqual(sanitize_filename("invoice\u202egnp.exe"),
                         "invoicegnp.exe")

    def test_zero_width_and_bom_stripped(self):
        self.assertEqual(sanitize_filename("a\u200bb\ufeff.txt"), "ab.txt")

    def test_control_characters_stripped(self):
        self.assertEqual(sanitize_filename("bad\x07\x1bname.txt"), "badname.txt")

    def test_override_cannot_hide_a_traversal(self):
        self.assertEqual(sanitize_filename("\u202e/../etc/passwd"), "passwd")

    def test_legitimate_unicode_survives(self):
        self.assertEqual(sanitize_filename("résumé final.pdf"), "résumé final.pdf")
        self.assertEqual(sanitize_filename("报告.txt"), "报告.txt")

    def test_name_of_only_dots_rejected(self):
        for bad in (".", "..", "..."):
            with self.assertRaises(ValueError):
                sanitize_filename(bad)

    def test_username_rejects_markup(self):
        for bad in ("<b>admin</b>", 'a"b', "a<b", "a>b"):
            with self.assertRaises(ValueError):
                validate_username(bad)

    def test_username_rejects_homoglyphs(self):
        # Cyrillic 'о' (U+043E) renders identically to ASCII 'o'
        with self.assertRaises(ValueError):
            validate_username("B\u043eb")

    def test_username_rejects_spaces_and_edge_punctuation(self):
        for bad in ("ad min", "-bob", "bob-", ".bob", "bob."):
            with self.assertRaises(ValueError):
                validate_username(bad)

    def test_ordinary_usernames_still_work(self):
        for good in ("Bob", "alice_99", "a.b-c", "user-name.1"):
            self.assertEqual(validate_username(good), good)

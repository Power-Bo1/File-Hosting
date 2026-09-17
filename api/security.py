"""Pure auth & validation helpers.

No web-framework or database imports, so this module can be unit-tested
in isolation (only PyJWT required).
"""
import datetime
import hashlib
import os
import re
import secrets

import jwt

JWT_ALGORITHM = "HS256"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Characters that have no legitimate use in a filename and exist only to
# deceive: C0/C1 controls, the bidirectional overrides (U+202A-202E,
# U+2066-2069) and zero-width/BOM. The classic attack is RIGHT-TO-LEFT
# OVERRIDE: "invoice\u202Egnp.exe" renders as "invoiceexe.png" - the user
# sees an image, downloads a binary. Stripped rather than rejected, so a
# legitimate name with accents, spaces or CJK still works.
_DECEPTIVE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)

# Usernames are chosen, not given, so they can be strict. ASCII only kills
# homoglyph impersonation ("Bob" with a Cyrillic 'o' is a different user
# that renders identically); no markup characters means the header line
# "Signed in as X" cannot be spoofed even if escaping ever regressed.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{1,62}[A-Za-z0-9])$")


class AuthError(Exception):
    pass


class TokenExpired(AuthError):
    pass


class TokenInvalid(AuthError):
    pass


def new_jti() -> str:
    """Unique id for one token, so a single session can be revoked."""
    return secrets.token_urlsafe(16)


def make_token(secret: str, user_id: int, username: str,
               ttl_minutes: int, ver: int = 0) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "ver": ver,
        "jti": new_jti(),
        "iat": now,
        "exp": now + datetime.timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_token(secret: str, token: str) -> dict:
    try:
        p = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        # jti defaults to "" so pre-v2.8 tokens still validate; they simply
        # cannot be individually revoked (they expire within TOKEN_TTL).
        return {"id": int(p["sub"]), "username": p["username"],
                "ver": int(p.get("ver", 0)),
                "jti": p.get("jti") or "",
                "exp": int(p["exp"])}
    except jwt.ExpiredSignatureError as e:
        raise TokenExpired("Token expired") from e
    except (jwt.InvalidTokenError, KeyError, ValueError, TypeError) as e:
        raise TokenInvalid("Invalid token") from e


def new_reset_token() -> tuple:
    """Return (token, sha256_hash). Only the HASH is ever stored -
    a leaked database cannot be turned into usable reset links."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sanitize_filename(name) -> str:
    """Strip directories and deceptive characters; reject empty/'.'/'..'.

    Order matters: strip the invisible characters FIRST, then basename.
    A name like "a\u202e/../b" must not have its separators hidden from
    the traversal check by an override sitting in front of them.
    """
    name = _DECEPTIVE.sub("", name or "")
    name = os.path.basename(name).strip()
    if not name or set(name) <= {"."}:
        raise ValueError("Invalid filename")
    if len(name) > 255:
        raise ValueError("Filename too long")
    return name


def validate_username(u) -> str:
    u = (u or "").strip()
    if not (3 <= len(u) <= 64):
        raise ValueError("Username must be 3-64 characters")
    if not _USERNAME_RE.match(u):
        raise ValueError(
            "Username may use letters, digits, dot, underscore and hyphen, "
            "and must start and end with a letter or digit")
    return u


def validate_password(p) -> str:
    if not (8 <= len((p or "").encode()) <= 72):
        raise ValueError("Password must be 8-72 characters (72 bytes max)")
    return p


def validate_email(e) -> str:
    e = (e or "").strip().lower()
    if len(e) > 254 or not _EMAIL_RE.match(e):
        raise ValueError("Invalid email address")
    return e

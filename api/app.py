import datetime
import os
import time
from contextlib import asynccontextmanager, contextmanager

import bcrypt
import psycopg2
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from mailer import send_email
from security import (
    make_token, decode_token, new_reset_token, hash_token,
    sanitize_filename, validate_username, validate_password, validate_email,
    TokenExpired, TokenInvalid,
)

SAFE_DATA_ROOT = os.path.realpath("/data")
_raw_data_dir = os.environ.get("DATA_DIR", SAFE_DATA_ROOT)
DATA_DIR = os.path.realpath(_raw_data_dir)
if os.path.commonpath([SAFE_DATA_ROOT, DATA_DIR]) != SAFE_DATA_ROOT:
    raise RuntimeError("Invalid DATA_DIR: must be within /data")
os.makedirs(DATA_DIR, exist_ok=True)

JWT_SECRET = os.environ["JWT_SECRET"]
TOKEN_TTL_MINUTES = int(os.environ.get("TOKEN_TTL_MINUTES", "60"))
RESET_TTL_MINUTES = int(os.environ.get("RESET_TTL_MINUTES", "30"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://files.local")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
CHUNK = 1024 * 1024

# Constant-time-ish login even for unknown users (anti user-enumeration).
_DUMMY_HASH = bcrypt.hashpw(b"placeholder-not-a-real-password", bcrypt.gensalt())


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def db():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
    )


@contextmanager
def db_conn():
    """Commit on success, roll back on error, ALWAYS close (no conn leaks)."""
    conn = db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Idempotent migrations; safe to run on every start."""
    for attempt in range(30):
        try:
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                            "email TEXT UNIQUE")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                            "token_version INTEGER NOT NULL DEFAULT 0")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS files (
                        id SERIAL PRIMARY KEY,
                        client TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size BIGINT NOT NULL,
                        uploaded_at TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute(
                    "ALTER TABLE files ADD COLUMN IF NOT EXISTS "
                    "user_id INTEGER REFERENCES users(id)"
                )
                cur.execute("""
                    DELETE FROM files a USING files b
                    WHERE a.user_id IS NOT DISTINCT FROM b.user_id
                      AND a.filename = b.filename AND a.id < b.id
                """)
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS files_user_filename_uq "
                    "ON files (user_id, filename)"
                )
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS password_resets (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id)
                            ON DELETE CASCADE,
                        token_hash TEXT UNIQUE NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL,
                        used_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS password_resets_user_idx "
                            "ON password_resets (user_id)")
                # Per-token denylist. Rows are self-expiring: a revoked
                # jti only matters until the token would have expired
                # anyway, so the table stays bounded by tokens issued in
                # the last TOKEN_TTL_MINUTES, not by all logouts ever.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS revoked_tokens (
                        jti TEXT PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        expires_at TIMESTAMPTZ NOT NULL,
                        revoked_at TIMESTAMPTZ DEFAULT now()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS revoked_tokens_expires_idx "
                            "ON revoked_tokens (expires_at)")
                cur.execute("DELETE FROM revoked_tokens WHERE expires_at < now()")
            print("DB ready: schema ensured")
            return
        except Exception as e:
            print(f"DB not ready ({e}); retry {attempt + 1}/30")
            time.sleep(2)
    raise RuntimeError("Could not reach the database")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Client File API", lifespan=lifespan)
bearer = HTTPBearer()


class RegisterBody(BaseModel):
    username: str
    password: str
    email: str


class LoginBody(BaseModel):
    username: str
    password: str


class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    new_password: str


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        tok = decode_token(JWT_SECRET, creds.credentials)
    except TokenExpired:
        raise HTTPException(401, "Token expired")
    except TokenInvalid:
        raise HTTPException(401, "Invalid token")
    # Two revocation checks in ONE round trip - this endpoint already
    # paid for a DB lookup, so the denylist costs nothing extra:
    #   token_version : bumped by password reset  -> kills ALL sessions
    #   revoked_tokens: written by logout         -> kills THIS session
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT u.token_version, "
            "EXISTS (SELECT 1 FROM revoked_tokens r WHERE r.jti = %s) "
            "FROM users u WHERE u.id = %s",
            (tok["jti"], tok["id"]),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(401, "Invalid token")
    version, revoked = row[0], row[1]
    if revoked:
        raise HTTPException(401, "Session has been logged out")
    if version != tok["ver"]:
        raise HTTPException(401, "Session is no longer valid - please log in again")
    return tok


@app.get("/health")
def health():
    """Liveness: process is up."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness: process is up AND the database answers.

    Deliberately lightweight: autocommit means no transaction and no
    COMMIT round-trip to stall on, and db() carries a connect_timeout so
    a connection attempt can never hang past the probe's deadline.
    """
    conn = None
    try:
        conn = db()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
    except Exception:
        raise HTTPException(503, "database unavailable")
    finally:
        if conn is not None:
            conn.close()
    return {"status": "ready"}


@app.post("/auth/register", status_code=201)
def register(creds: RegisterBody):
    try:
        username = validate_username(creds.username)
        validate_password(creds.password)
        email = validate_email(creds.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    pw_hash = bcrypt.hashpw(creds.password.encode(), bcrypt.gensalt()).decode()
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (username, password_hash, email) "
                "VALUES (%s, %s, %s) RETURNING id",
                (username, pw_hash, email),
            )
            user_id = cur.fetchone()[0]
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(409, "Username or email already taken")
    print(f"registered user {username} (id={user_id})")
    return {"id": user_id, "username": username}


@app.post("/auth/login")
def login(creds: LoginBody):
    username = (creds.username or "").strip()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, password_hash, token_version FROM users "
            "WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    stored = row[1].encode() if row else _DUMMY_HASH
    ok = bcrypt.checkpw((creds.password or "").encode(), stored)
    if not row or not ok:
        raise HTTPException(401, "Invalid username or password")
    print(f"login ok for {username}")
    return {
        "access_token": make_token(JWT_SECRET, row[0], username,
                                   TOKEN_TTL_MINUTES, ver=row[2]),
        "token_type": "bearer",
    }


@app.post("/auth/forgot")
def forgot(body: ForgotBody):
    try:
        email = validate_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    generic = {"message": "If that email is registered, a reset link is on its way."}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
    if not row:
        # Same answer as success: never reveal which emails exist.
        return generic
    user_id, username = row
    token, token_hash = new_reset_token()
    expires = _utcnow() + datetime.timedelta(minutes=RESET_TTL_MINUTES)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM password_resets WHERE expires_at < %s", (_utcnow(),))
        cur.execute("DELETE FROM password_resets WHERE user_id = %s", (user_id,))
        cur.execute(
            "INSERT INTO password_resets (user_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s)",
            (user_id, token_hash, expires),
        )
    link = f"{APP_BASE_URL}/reset?token={token}"
    try:
        send_email(
            to=email,
            subject="Reset your Client File Host password",
            body=(f"Hi {username},\n\n"
                  f"Someone requested a password reset for your account.\n"
                  f"This link is valid for {RESET_TTL_MINUTES} minutes and "
                  f"works exactly once:\n\n{link}\n\n"
                  f"If you didn't request this, you can ignore this email - "
                  f"your password is unchanged."),
        )
    except Exception as e:
        print(f"WARNING: failed to send reset email: {e}")
        raise HTTPException(503, "Email service unavailable - try again later")
    # NEVER log the token/link itself: logs are shipped to Loki.
    print(f"password reset link issued for {username}")
    return generic


@app.post("/auth/reset")
def reset(body: ResetBody):
    try:
        validate_password(body.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    invalid = HTTPException(400, "Invalid or expired reset link")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pr.id, pr.user_id, pr.expires_at, pr.used_at, "
            "u.username, u.email "
            "FROM password_resets pr JOIN users u ON u.id = pr.user_id "
            "WHERE pr.token_hash = %s",
            (hash_token(body.token),),
        )
        row = cur.fetchone()
    if not row:
        raise invalid
    pr_id, user_id, expires_at, used_at, username, email = row
    if used_at is not None or expires_at < _utcnow():
        raise invalid
    new_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET password_hash = %s, "
            "token_version = token_version + 1 WHERE id = %s",
            (new_hash, user_id),
        )
        cur.execute(
            "UPDATE password_resets SET used_at = %s WHERE id = %s",
            (_utcnow(), pr_id),
        )
    try:
        send_email(
            to=email,
            subject="Your Client File Host password was changed",
            body=(f"Hi {username},\n\n"
                  f"Your password was just changed and all active sessions "
                  f"were logged out.\n\n"
                  f"If this wasn't you, reset your password again immediately "
                  f"using 'Forgot password?' on the login page."),
        )
    except Exception as e:
        print(f"WARNING: failed to send confirmation email: {e}")
    print(f"password reset completed for {username}; all sessions invalidated")
    return {"message": "Password updated. Please log in with your new password."}


@app.post("/auth/logout")
def logout(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    """Revoke exactly this token. Other sessions for the same user keep
    working - that is the difference from a token_version bump."""
    try:
        tok = decode_token(JWT_SECRET, creds.credentials)
    except TokenExpired:
        # Already dead; nothing to store. Treat as success so a client
        # logging out with a stale token still gets a clean answer.
        return {"message": "Logged out"}
    except TokenInvalid:
        raise HTTPException(401, "Invalid token")

    if not tok["jti"]:
        # Pre-v2.8 token with no jti: cannot be revoked individually.
        # It expires on its own within TOKEN_TTL_MINUTES.
        print(f"logout for {tok['username']} (legacy token, no jti to revoke)")
        return {"message": "Logged out"}

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM revoked_tokens WHERE expires_at < now()")
        cur.execute(
            "INSERT INTO revoked_tokens (jti, user_id, expires_at) "
            "VALUES (%s, %s, to_timestamp(%s)) ON CONFLICT (jti) DO NOTHING",
            (tok["jti"], tok["id"], tok["exp"]),
        )
    print(f"logout for {tok['username']}; token revoked")
    return {"message": "Logged out"}


@app.post("/upload")
async def upload(file: UploadFile = File(...), user=Depends(current_user)):
    try:
        fname = sanitize_filename(file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))

    user_dir = os.path.join(DATA_DIR, str(user["id"]))
    os.makedirs(user_dir, exist_ok=True)
    dest = os.path.join(user_dir, fname)
    tmp = dest + ".part"

    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        413, f"File exceeds the {MAX_UPLOAD_MB} MB limit"
                    )
                out.write(chunk)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    os.replace(tmp, dest)  # atomic: never a half-written file at `dest`

    try:
        with db_conn() as conn:
            conn.cursor().execute(
                "INSERT INTO files (client, filename, size, user_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, filename) DO UPDATE "
                "SET size = EXCLUDED.size, uploaded_at = now(), "
                "    client = EXCLUDED.client",
                (user["username"], fname, written, user["id"]),
            )
    except Exception as e:
        print(f"WARNING: {fname} saved to disk but DB record failed: {e}")
        raise HTTPException(500, "File stored but not recorded; please retry")

    print(f"stored {fname} for {user['username']} ({written} bytes)")
    return {"stored": fname, "size": written}


@app.get("/files/{filename}")
def download_file(filename: str, user=Depends(current_user)):
    """Serve one of the caller's own files.

    Ownership is decided by the DATABASE, not by the path: we look the
    row up as (user_id, filename), so there is no way to reach another
    user's directory even if the name survived sanitising.
    """
    try:
        fname = sanitize_filename(filename)
    except ValueError:
        raise HTTPException(400, "Invalid filename")

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT size FROM files WHERE user_id = %s AND filename = %s",
            (user["id"], fname),
        )
        row = cur.fetchone()
    # 404 (not 403) for someone else's file: never confirm it exists.
    if not row:
        raise HTTPException(404, "File not found")

    path = os.path.join(DATA_DIR, str(user["id"]), fname)
    if not os.path.isfile(path):
        print(f"WARNING: {fname} is recorded for user {user['id']} "
              f"but missing on disk")
        raise HTTPException(404, "File not found")

    print(f"download {fname} by {user['username']}")
    # application/octet-stream + attachment: an uploaded .html or .svg is
    # NEVER rendered in the origin, so it cannot script against the app.
    return FileResponse(path, media_type="application/octet-stream",
                        filename=fname)


@app.delete("/files/{filename}")
def delete_file(filename: str, user=Depends(current_user)):
    """Delete one of the caller's own files, row and blob together."""
    try:
        fname = sanitize_filename(filename)
    except ValueError:
        raise HTTPException(400, "Invalid filename")

    path = os.path.join(DATA_DIR, str(user["id"]), fname)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM files WHERE user_id = %s AND filename = %s RETURNING id",
            (user["id"], fname),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, "File not found")
        # Unlink INSIDE the transaction: if it raises, db_conn rolls back
        # and the row survives, so the listing never shows a file whose
        # bytes are already gone. The reverse order could strand a row.
        if os.path.isfile(path):
            os.remove(path)

    print(f"deleted {fname} for {user['username']}")
    return {"deleted": fname}


@app.get("/files")
def list_files(user=Depends(current_user)):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT filename, size, uploaded_at FROM files "
            "WHERE user_id = %s ORDER BY uploaded_at DESC",
            (user["id"],),
        )
        rows = [
            {"filename": r[0], "size": r[1], "uploaded_at": str(r[2])}
            for r in cur.fetchall()
        ]
    return rows

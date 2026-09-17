import hashlib
import os

import secrets
from urllib.parse import quote

import requests
from flask import (
    Flask, Response, request, render_template_string, redirect,
    session, url_for, flash,
)

API_URL = os.environ.get("API_URL", "http://api:8000")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config["SESSION_COOKIE_HTTPONLY"] = True
# __Host- is browser-ENFORCED: the cookie is rejected unless it is Secure,
# Path=/ and has no Domain attribute. That means a compromised or
# attacker-controlled sibling host cannot overwrite our session cookie -
# the classic session-fixation-by-cookie-injection route. Only used when
# COOKIE_SECURE is on, since the prefix requires HTTPS.
if os.environ.get("COOKIE_SECURE", "false").lower() == "true":
    app.config["SESSION_COOKIE_NAME"] = "__Host-session"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"   # blocks cross-site POSTs (CSRF)
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "false").lower() == "true"
)
# Reject oversized uploads at the edge instead of buffering them:
app.config["MAX_CONTENT_LENGTH"] = (MAX_UPLOAD_MB + 1) * 1024 * 1024

# ---------------------------------------------------------------------------
# Presentation. Deliberately ZERO JavaScript: the ingress CSP sets
# script-src 'none', so a <script> tag would simply be blocked. Everything
# below - dark mode, focus rings, hover states, the responsive layout, even
# the favicon - is plain CSS and semantic HTML. No external stylesheet or
# webfont either: default-src 'self' forbids them, and a system font stack
# is faster and leaks nothing to a CDN.
# ---------------------------------------------------------------------------

# Inline SVG favicon as a data: URI - allowed by img-src 'self' data:.
# Also removes the /favicon.ico 404 that every page load used to generate.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%232563eb'/%3E"
    "%3Cpath d='M11 7h7l5 5v13a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z'"
    " fill='%23fff'/%3E%3Cpath d='M18 7v5h5' fill='%23bfdbfe'/%3E%3C/svg%3E"
)

STYLES = """
:root{
  color-scheme: light dark;
  --bg:#f5f6f8; --surface:#fff; --surface-2:#fafbfc; --border:#e3e6ea;
  --text:#15181d; --muted:#6b7280; --accent:#2563eb; --accent-ink:#fff;
  --accent-weak:#eff4ff; --danger:#dc2626; --danger-weak:#fef2f2;
  --ok:#047857; --ok-weak:#ecfdf5;
  --radius:10px; --gap:1rem;
  --shadow:0 1px 2px rgba(16,24,40,.05), 0 6px 16px -6px rgba(16,24,40,.10);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0e1116; --surface:#161a21; --surface-2:#1b2029; --border:#272d38;
    --text:#e7e9ec; --muted:#98a2b3; --accent:#3b82f6; --accent-weak:#16233b;
    --danger:#f87171; --danger-weak:#2a1717; --ok:#34d399; --ok-weak:#0f2620;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 20px -8px rgba(0,0,0,.6);
  }
}
*,*::before,*::after{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
       "Helvetica Neue",Arial,"Noto Sans",sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--accent); text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:4px}

.site-header{
  background:var(--surface); border-bottom:1px solid var(--border);
  position:sticky; top:0; z-index:5;
}
.site-header .inner{
  max-width:920px; margin:0 auto; padding:.75rem 1.25rem;
  display:flex; align-items:center; gap:1rem;
}
.brand{display:flex; align-items:center; gap:.55rem; font-weight:650;
  color:var(--text); letter-spacing:-.01em}
.brand:hover{text-decoration:none}
.brand svg{display:block}
.spacer{flex:1}
.who{display:flex; align-items:center; gap:.5rem; color:var(--muted); font-size:.9rem}
.avatar{
  width:26px; height:26px; border-radius:50%; background:var(--accent);
  color:var(--accent-ink); display:grid; place-items:center;
  font-size:.75rem; font-weight:700; text-transform:uppercase;
}
.navlink{color:var(--muted); font-size:.9rem}
.navlink:hover{color:var(--text)}

main{max-width:920px; margin:0 auto; padding:1.75rem 1.25rem 3rem}
.auth{max-width:400px; margin:3.5rem auto; padding:0 1.25rem}
h1{font-size:1.35rem; margin:0 0 .35rem; letter-spacing:-.02em}
h1.page{margin-bottom:1.1rem}
.sub{color:var(--muted); margin:0 0 1.25rem; font-size:.92rem}

.card{
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); box-shadow:var(--shadow);
  padding:1.4rem; margin-bottom:1.25rem;
}
.card h2{font-size:.95rem; margin:0 0 1rem; letter-spacing:-.01em}

.field{display:block; margin-bottom:.9rem}
.field > span{display:block; font-size:.82rem; font-weight:600;
  color:var(--muted); margin-bottom:.35rem}
input[type=text],input[type=password],input[type=email],input[type=file]{
  width:100%; padding:.6rem .7rem; font:inherit; color:var(--text);
  background:var(--surface-2); border:1px solid var(--border);
  border-radius:8px; transition:border-color .12s, box-shadow .12s;
}
input:focus{outline:none; border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-weak)}
input[type=file]{padding:.45rem}
input[type=file]::file-selector-button{
  font:inherit; margin-right:.7rem; padding:.35rem .8rem; cursor:pointer;
  color:var(--text); background:var(--surface); border:1px solid var(--border);
  border-radius:6px;
}
input[type=file]::file-selector-button:hover{border-color:var(--accent)}

.btn{
  display:inline-block; font:inherit; font-weight:600; cursor:pointer;
  padding:.55rem 1.05rem; border-radius:8px; border:1px solid transparent;
  transition:background .12s, border-color .12s, color .12s;
}
.btn-primary{background:var(--accent); color:var(--accent-ink)}
.btn-primary:hover{filter:brightness(.93)}
.btn-danger{background:var(--danger); color:#fff}
.btn-danger:hover{filter:brightness(.93)}
.btn-ghost{background:transparent; color:var(--text); border-color:var(--border)}
.btn-ghost:hover{border-color:var(--accent); color:var(--accent);
  text-decoration:none}
.btn-block{width:100%}
.row{display:flex; gap:.6rem; align-items:center; flex-wrap:wrap}

.alert{
  display:flex; gap:.6rem; align-items:flex-start; padding:.7rem .9rem;
  border-radius:8px; border:1px solid; margin-bottom:1rem; font-size:.92rem;
}
.alert svg{flex:none; margin-top:.15rem}
.alert.err{background:var(--danger-weak); border-color:var(--danger); color:var(--danger)}
.alert.msg{background:var(--ok-weak); border-color:var(--ok); color:var(--ok)}

.table-wrap{overflow-x:auto; margin:0 -.35rem}
table{width:100%; border-collapse:collapse; font-size:.92rem}
thead th{
  text-align:left; font-size:.74rem; text-transform:uppercase;
  letter-spacing:.05em; color:var(--muted); font-weight:700;
  padding:.5rem .7rem; border-bottom:1px solid var(--border); white-space:nowrap;
}
tbody td{padding:.7rem; border-bottom:1px solid var(--border); vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface-2)}
.fname{display:flex; align-items:center; gap:.55rem; font-weight:550;
  word-break:break-all}
.fname svg{flex:none; color:var(--muted)}
.num{color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums}
.actions{text-align:right; white-space:nowrap}
.actions a{font-size:.86rem; font-weight:600; padding:.3rem .55rem;
  border-radius:6px; display:inline-block}
.actions a:hover{background:var(--accent-weak); text-decoration:none}
.actions a.del{color:var(--danger)}
.actions a.del:hover{background:var(--danger-weak)}

.empty{text-align:center; padding:2.5rem 1rem; color:var(--muted)}
.empty svg{opacity:.45; margin-bottom:.6rem}
.hint{font-size:.85rem; color:var(--muted); margin:.75rem 0 0}
.center{text-align:center}
footer{max-width:920px; margin:0 auto; padding:0 1.25rem 2.5rem;
  color:var(--muted); font-size:.8rem}
.warnbox{background:var(--danger-weak); border:1px solid var(--danger);
  border-radius:8px; padding:.9rem 1rem; margin-bottom:1.2rem}
.warnbox code{background:transparent; font-weight:700; word-break:break-all}

@media (max-width:560px){
  main{padding:1.25rem .9rem 2rem}
  .card{padding:1.1rem}
  .site-header .inner{padding:.65rem .9rem}
  .who .name{display:none}
}
"""

ICON_FILE = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
             'stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
             '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
             '<path d="M14 3v5h5"/></svg>')

HEAD = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{{ title|default("") }}Client File Host</title>
<link rel="icon" href=\"""" + FAVICON + """\">
<link rel="stylesheet" href="/style.css?v={{ style_version }}">
</head>
<body>
<header class="site-header"><div class="inner">
  <a class="brand" href="{{ url_for('index') }}">
    <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#2563eb"/>
      <path d="M11 7h7l5 5v13a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z" fill="#fff"/>
      <path d="M18 7v5h5" fill="#bfdbfe"/>
    </svg>
    Client File Host
  </a>
  <span class="spacer"></span>
  {% if session.get('token') %}
    <span class="who">
      <span class="avatar" aria-hidden="true">{{ session['username'][:1] }}</span>
      <span class="name">{{ session['username'] }}</span>
    </span>
    <a class="navlink" href="{{ url_for('logout') }}">Log out</a>
  {% else %}
    <a class="navlink" href="{{ url_for('login') }}">Log in</a>
    <a class="navlink" href="{{ url_for('register') }}">Register</a>
  {% endif %}
</div></header>
"""

FLASHES = """
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for cat, m in messages %}
  <div class="alert {{ cat }}" role="alert">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/>
      <path d="M12 8v5M12 16.5v.01"/></svg>
    <div>{{ m }}</div>
  </div>
  {% endfor %}
{% endwith %}
"""

FOOT = """
<footer>Client File Host &middot; files are private to your account</footer>
</body></html>
"""

PAGE_HOME = '{% set title = "Your files - " %}' + HEAD + """
<main>
""" + FLASHES + """
<h1 class="page">Your files</h1>

<div class="card">
  <h2>Upload a file</h2>
  <form method="post" enctype="multipart/form-data" action="{{ url_for('upload') }}">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label class="field"><span>Choose a file</span>
      <input type="file" name="file" required>
    </label>
    <button class="btn btn-primary" type="submit">Upload</button>
    <p class="hint">Maximum {{ max_mb }} MB per file.</p>
  </form>
</div>

<div class="card">
  <h2>{{ files|length }} file{{ '' if files|length == 1 else 's' }} stored</h2>
  {% if files %}
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>Name</th><th>Size</th><th>Uploaded</th><th class="actions">Actions</th>
    </tr></thead>
    <tbody>
    {% for f in files %}
      <tr>
        <td><span class="fname">""" + ICON_FILE + """{{ f.filename }}</span></td>
        <td class="num">{{ f.size_h }}</td>
        <td class="num">{{ f.date_h }}</td>
        <td class="actions">
          <a href="{{ url_for('download', filename=f.filename) }}">Download</a>
          <a class="del" href="{{ url_for('confirm_delete', filename=f.filename) }}">Delete</a>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.5" aria-hidden="true">
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>
      <path d="M14 3v5h5"/></svg>
    <div>No files yet — upload one above to get started.</div>
  </div>
  {% endif %}
</div>
</main>
""" + FOOT

PAGE_LOGIN = '{% set title = "Log in - " %}' + HEAD + """
<div class="auth">
""" + FLASHES + """
<h1>Welcome back</h1>
<p class="sub">Sign in to reach your files.</p>
<div class="card">
  <form method="post">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label class="field"><span>Username</span>
      <input type="text" name="username" autocomplete="username" required autofocus>
    </label>
    <label class="field"><span>Password</span>
      <input type="password" name="password" autocomplete="current-password" required>
    </label>
    <button class="btn btn-primary btn-block" type="submit">Log in</button>
  </form>
</div>
<p class="center sub"><a href="{{ url_for('forgot') }}">Forgot password?</a>
   &nbsp;&middot;&nbsp; <a href="{{ url_for('register') }}">Create an account</a></p>
</div>
""" + FOOT

PAGE_REGISTER = '{% set title = "Register - " %}' + HEAD + """
<div class="auth">
""" + FLASHES + """
<h1>Create your account</h1>
<p class="sub">You will need the email address to reset your password.</p>
<div class="card">
  <form method="post">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label class="field"><span>Username</span>
      <input type="text" name="username" autocomplete="username" minlength="3"
             maxlength="64" required autofocus>
    </label>
    <label class="field"><span>Email</span>
      <input type="email" name="email" autocomplete="email" required>
    </label>
    <label class="field"><span>Password</span>
      <input type="password" name="password" autocomplete="new-password"
             minlength="8" required>
    </label>
    <button class="btn btn-primary btn-block" type="submit">Create account</button>
  </form>
</div>
<p class="center sub">Already registered?
   <a href="{{ url_for('login') }}">Log in</a></p>
</div>
""" + FOOT

PAGE_FORGOT = '{% set title = "Forgot password - " %}' + HEAD + """
<div class="auth">
""" + FLASHES + """
<h1>Forgot password</h1>
<p class="sub">Enter your registered email and we'll send a reset link.
   It is valid for 30 minutes and works once.</p>
<div class="card">
  <form method="post">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label class="field"><span>Email</span>
      <input type="email" name="email" autocomplete="email" required autofocus>
    </label>
    <button class="btn btn-primary btn-block" type="submit">Send reset link</button>
  </form>
</div>
<p class="center sub"><a href="{{ url_for('login') }}">Back to log in</a></p>
</div>
""" + FOOT

PAGE_RESET = '{% set title = "Choose a new password - " %}' + HEAD + """
<div class="auth">
""" + FLASHES + """
<h1>Choose a new password</h1>
<p class="sub">Setting a new password signs you out everywhere else.</p>
<div class="card">
  <form method="post">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="token" value="{{ token }}">
    <label class="field"><span>New password</span>
      <input type="password" name="new_password" autocomplete="new-password"
             minlength="8" required autofocus>
    </label>
    <label class="field"><span>Confirm password</span>
      <input type="password" name="confirm_password" autocomplete="new-password"
             minlength="8" required>
    </label>
    <button class="btn btn-primary btn-block" type="submit">Set new password</button>
  </form>
</div>
</div>
""" + FOOT

PAGE_CONFIRM_DELETE = '{% set title = "Confirm delete - " %}' + HEAD + """
<main>
""" + FLASHES + """
<h1 class="page">Delete this file?</h1>
<div class="card">
  <div class="warnbox">
    You are about to permanently delete <code>{{ filename }}</code>.
    This cannot be undone.
  </div>
  <div class="row">
    <form method="post">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <button class="btn btn-danger" type="submit">Yes, delete it</button>
    </form>
    <a class="btn btn-ghost" href="{{ url_for('index') }}">Cancel</a>
  </div>
</div>
</main>
""" + FOOT


PAGE_CSRF_FAIL = '{% set title = "Request blocked - " %}' + HEAD + """
<h1>Request blocked</h1>
<p>This request could not be verified as coming from a page on this site,
   so it was refused. This normally means the form was left open too long
   or was submitted from somewhere else.</p>
<p><a href="{{ url_for('index') }}">Return to your files</a></p>
""" + FOOT


def human_size(n):
    """Bytes -> a readable string. Rendering detail, kept out of the API."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_date(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value)).strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return str(value)


def decorate(files):
    """Add display-only fields; leave the API's data untouched."""
    out = []
    for f in files:
        g = dict(f)
        g["size_h"] = human_size(g.get("size"))
        g["date_h"] = human_date(g.get("uploaded_at"))
        out.append(g)
    return out


# Hash of the stylesheet, used both as the ETag and as a cache-busting
# query string, so a deploy invalidates the browser cache immediately.
STYLE_HASH = hashlib.sha256(STYLES.encode()).hexdigest()[:12]


@app.context_processor
def inject_style_version():
    return {"style_version": STYLE_HASH}


@app.get("/style.css")
def stylesheet():
    """Same-origin stylesheet.

    Serving CSS from a URL instead of an inline <style> block is what
    lets the CSP drop 'unsafe-inline' from style-src - the one genuinely
    weak directive in the policy. Nothing here is user-controlled.
    """
    etag = f'"{STYLE_HASH}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})
    return Response(STYLES, mimetype="text/css",
                    headers={"ETag": etag,
                             "Cache-Control": "public, max-age=3600"})


def api_request(method, path, **kwargs):
    """Call the API; return a Response, or None if the API is unreachable."""
    timeout = kwargs.pop("timeout", 5)
    try:
        return requests.request(method, f"{API_URL}{path}", timeout=timeout, **kwargs)
    except requests.RequestException:
        return None


def api_detail(r, default):
    """Pull a 'detail' message out of an API response without ever raising."""
    try:
        return r.json().get("detail", default)
    except ValueError:
        return default


# ---------------------------------------------------------------------
# CSRF: a synchroniser token, verified on EVERY state-changing request.
#
# SameSite=Lax already blocks cross-site POSTs in current browsers, but
# it is one mechanism, enforced by the client, and it does not help
# against a same-site attacker (a compromised sibling host is still
# "same site"). The token is the defence that does not depend on either.
#
# It lives in the Flask session, which is a signed cookie - so the token
# cannot be forged without the app secret, and an attacker who can read
# it already has the session.
# ---------------------------------------------------------------------
CSRF_FIELD = "_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.context_processor
def inject_csrf():
    return {"csrf_token": csrf_token}


@app.before_request
def verify_csrf():
    if request.method in SAFE_METHODS:
        return None
    expected = session.get("csrf", "")
    supplied = request.form.get(CSRF_FIELD, "")
    if not expected or not secrets.compare_digest(str(supplied), str(expected)):
        # 400, not a redirect: a forged request should fail loudly rather
        # than bounce the victim somewhere that looks like it worked.
        return render_template_string(PAGE_CSRF_FAIL), 400
    return None


@app.after_request
def security_response_headers(resp):
    # Authenticated HTML lists someone's private filenames. no-store keeps
    # it out of the browser's disk cache and out of any shared proxy, so
    # it cannot be recovered from the machine after logout.
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


def auth_headers():
    return {"Authorization": f"Bearer {session['token']}"}


def session_expired():
    session.clear()
    flash("Session expired - please log in again.", "err")
    return redirect(url_for("login"))


@app.errorhandler(413)
def too_large(_e):
    flash(f"File exceeds the {MAX_UPLOAD_MB} MB limit.", "err")
    return redirect(url_for("index"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.route("/", methods=["GET"])
def index():
    if not session.get("token"):
        return redirect(url_for("login"))
    r = api_request("GET", "/files", headers=auth_headers())
    if r is None:
        flash("Cannot reach the API right now - showing nothing.", "err")
        return render_template_string(PAGE_HOME, files=[], max_mb=MAX_UPLOAD_MB)
    if r.status_code == 401:
        return session_expired()
    if r.status_code != 200:
        flash("Could not load your files.", "err")
        return render_template_string(PAGE_HOME, files=[], max_mb=MAX_UPLOAD_MB)
    return render_template_string(PAGE_HOME, files=decorate(r.json()),
                                  max_mb=MAX_UPLOAD_MB)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template_string(PAGE_REGISTER)
    r = api_request("POST", "/auth/register", json={
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
        "email": request.form.get("email", ""),
    })
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
        return render_template_string(PAGE_REGISTER)
    if r.status_code == 201:
        flash("Account created - you can log in now.", "msg")
        return redirect(url_for("login"))
    flash(api_detail(r, "Registration failed"), "err")
    return render_template_string(PAGE_REGISTER)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template_string(PAGE_LOGIN)
    r = api_request("POST", "/auth/login", json={
        "username": request.form.get("username", ""),
        "password": request.form.get("password", ""),
    })
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
        return render_template_string(PAGE_LOGIN)
    if r.status_code == 200:
        # Start a fresh session on privilege change: anything an
        # unauthenticated visitor put there is discarded, and the CSRF
        # token is regenerated so a pre-login token cannot be reused.
        session.clear()
        session["token"] = r.json()["access_token"]
        session["username"] = request.form.get("username", "").strip()
        csrf_token()
        return redirect(url_for("index"))
    flash(api_detail(r, "Login failed"), "err")
    return render_template_string(PAGE_LOGIN)


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "GET":
        return render_template_string(PAGE_FORGOT)
    r = api_request("POST", "/auth/forgot",
                    json={"email": request.form.get("email", "")})
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
        return render_template_string(PAGE_FORGOT)
    if r.status_code == 200:
        flash(r.json().get("message",
                           "If that email is registered, a reset link is on its way."),
              "msg")
        return redirect(url_for("login"))
    flash(api_detail(r, "Could not start the reset - try again later."), "err")
    return render_template_string(PAGE_FORGOT)


@app.route("/reset", methods=["GET", "POST"])
def reset():
    if request.method == "GET":
        token = request.args.get("token", "")
        if not token:
            flash("That reset link is missing its token - request a new one.", "err")
            return redirect(url_for("forgot"))
        return render_template_string(PAGE_RESET, token=token)
    token = request.form.get("token", "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if new_pw != confirm:
        flash("Passwords do not match.", "err")
        return render_template_string(PAGE_RESET, token=token)
    r = api_request("POST", "/auth/reset",
                    json={"token": token, "new_password": new_pw})
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
        return render_template_string(PAGE_RESET, token=token)
    if r.status_code == 200:
        flash(r.json().get("message", "Password updated - log in with it now."), "msg")
        return redirect(url_for("login"))
    flash(api_detail(r, "Reset failed - the link may have expired."), "err")
    return render_template_string(PAGE_RESET, token=token)


@app.get("/download/<filename>")
def download(filename):
    if not session.get("token"):
        return redirect(url_for("login"))
    # stream=True: a 100 MB download must not be buffered in the web pod
    r = api_request("GET", f"/files/{quote(filename)}",
                    headers=auth_headers(), stream=True, timeout=120)
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
        return redirect(url_for("index"))
    if r.status_code == 401:
        return session_expired()
    if r.status_code != 200:
        flash("Could not download that file.", "err")
        return redirect(url_for("index"))
    return Response(
        r.iter_content(chunk_size=64 * 1024),
        content_type=r.headers.get("Content-Type", "application/octet-stream"),
        headers={"Content-Disposition": r.headers.get(
            "Content-Disposition", f'attachment; filename="{filename}"')},
    )


@app.route("/delete/<filename>", methods=["GET", "POST"])
def confirm_delete(filename):
    if not session.get("token"):
        return redirect(url_for("login"))
    # GET only renders a confirmation page - it has no side effect, so a
    # prefetcher or crawler following the link cannot delete anything.
    # The destructive step is POST, which SameSite=Lax blocks cross-site.
    if request.method == "GET":
        return render_template_string(PAGE_CONFIRM_DELETE, filename=filename)
    r = api_request("DELETE", f"/files/{quote(filename)}", headers=auth_headers())
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
    elif r.status_code == 401:
        return session_expired()
    elif r.status_code == 200:
        flash(f"Deleted {filename}.", "msg")
    else:
        flash(api_detail(r, "Could not delete that file."), "err")
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    # Tell the API to revoke THIS token before dropping the cookie.
    # Best effort: if the API is unreachable the local session is still
    # cleared, and the token expires on its own within TOKEN_TTL_MINUTES.
    if session.get("token"):
        api_request("POST", "/auth/logout", headers=auth_headers())
    session.clear()
    flash("Logged out.", "msg")
    return redirect(url_for("login"))


@app.post("/upload")
def upload():
    if not session.get("token"):
        return redirect(url_for("login"))
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("Choose a file first.", "err")
        return redirect(url_for("index"))
    r = api_request(
        "POST", "/upload",
        headers=auth_headers(),
        files={"file": (f.filename, f.stream, f.mimetype)},
        timeout=120,
    )
    if r is None:
        flash("Cannot reach the API right now - try again shortly.", "err")
    elif r.status_code == 401:
        return session_expired()
    elif r.status_code != 200:
        flash(api_detail(r, "Upload failed."), "err")
    return redirect(url_for("index"))

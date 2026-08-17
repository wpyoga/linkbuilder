# Import standard library modules for OS path operations, file manipulation, and JSON handling
import os
import re
import io
import json
import shutil
import secrets
import sqlite3

from contextlib import contextmanager
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask import session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

# Pillow is used to verify that uploaded raster images are actually what their extension
# claims (magic-byte / structural verification, not filename-extension trust).
from PIL import Image, UnidentifiedImageError
from email_validator import EmailNotValidError, validate_email
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException

# defusedxml protects against XML attacks (XXE, billion-laughs, external entity expansion)
# while we parse uploaded SVGs in order to sanitize them.
import defusedxml.ElementTree as DefusedET

# Initialize the main Flask application instance
app = Flask(__name__)

# Resolve the path to the secret key file. The key file is generated once by `init-db`
# and read here at every startup. It can be overridden by an env var for deployments that
# prefer to manage secrets externally (e.g. a secrets manager that injects a file path).
SECRET_KEY_FILE = os.environ.get(
    "SECRET_KEY_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret_key"),
)

# Load the secret key. We deliberately do NOT fall back to os.urandom() here.
#
# The silent-random-fallback pattern is a common Flask boilerplate mistake: it looks safe
# because each process gets "a random key," but across multiple gunicorn workers each worker
# generates its OWN random key at startup. Worker A signs the session cookie on GET /login;
# worker B verifies it on POST /login with a different key; the signature fails; the session
# reads as empty; flask-wtf finds no csrf_token and returns 400 "CSRF session token is
# missing" -- intermittently, depending on which worker the OS schedules for each request.
# The symptom is especially confusing because it only reliably surfaces after a wrong-
# password attempt (the original session cookie stays in play across that retry), while a
# correct-password first try usually succeeds (login_user issues a new cookie signed by
# whatever worker handled the POST, making subsequent requests self-consistent).
#
# Instead: require a stable key file written by `init-db`. If the file is absent the app
# refuses to start with a clear message, rather than silently misbehaving under load.
if os.environ.get("FLASK_SECRET_KEY"):
    # Explicit env var takes precedence -- useful for deployments that inject secrets via
    # environment (Docker secrets, systemd credentials, etc.) rather than files.
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
elif os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, "rb") as _f:
        app.secret_key = _f.read().strip()
    if not app.secret_key:
        raise RuntimeError(
            f"Secret key file {SECRET_KEY_FILE!r} exists but is empty. "
            "Delete it and re-run `flask init-db` to generate a fresh one."
        )
else:
    # Key file doesn't exist yet -- don't raise here. Raising at module level would prevent
    # `flask init-db` from running (it imports the module to find the CLI command, so a
    # module-level error blocks the very command that generates the key). Instead, set a
    # placeholder and enforce in before_request below, which fires on real HTTP requests
    # but not during CLI commands.
    app.secret_key = None


@app.before_request
def enforce_secret_key():
    # Raise here rather than at module level so `flask init-db` can still run to generate
    # the key file. Once the key is present, this hook reads it and installs it on the
    # first request, then removes itself so subsequent requests don't pay the file-read
    # overhead on every call.
    if app.secret_key is None:
        if os.path.exists(SECRET_KEY_FILE):
            # Key was generated (e.g. by init-db running in this same process) after module
            # load -- pick it up now.
            with open(SECRET_KEY_FILE, "rb") as _f:
                app.secret_key = _f.read().strip()
        else:
            raise RuntimeError(
                f"No secret key found. Run `flask init-db` to generate one, or set the "
                f"FLASK_SECRET_KEY environment variable. "
                f"Expected key file: {SECRET_KEY_FILE!r}"
            )


# Apply proxy fix middleware to handle HTTP headers correctly behind reverse proxies like Nginx
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Evaluate security boolean from environment variables for cookie handling
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")

# Apply Flask configuration parameters for session security and file size limits.
# TEMPLATES_AUTO_RELOAD is intentionally NOT set here: it defaults to Flask's DEBUG value,
# which is what we want. Forcing it True unconditionally (as the previous version did) adds
# a per-request filesystem stat() call for every template with no benefit outside development.
app.config.update(
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

# Enable CSRF protection for every state-changing view in this app. flask-wtf's CSRFProtect
# hooks into Flask's request lifecycle and rejects any POST/PUT/PATCH/DELETE request that
# doesn't carry a valid csrf_token matching the one issued in the session. Every <form> that
# performs a mutation must therefore include {{ csrf_token() }} as a hidden field.
#
# Why this matters here specifically: SESSION_COOKIE_SAMESITE="Lax" is NOT sufficient CSRF
# protection on its own. Lax blocks cross-site *fetch/XHR* POSTs, but it has known gaps
# (e.g. Chrome's "Lax + POST" grace period allows top-level cross-site POST navigations for
# cookies less than ~2 minutes old) and inconsistent enforcement across browsers/versions.
# CSRFProtect closes that gap with an explicit, unguessable per-session token checked on
# every mutating request -- currently /login and /save.
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    logout_user()
    session.clear()
    flash("Your session has expired. Please log in again.", "danger")
    return redirect(url_for("login"))


# Resolve default paths for the SQLite database and the static export destination directory.
# DB_PATH is made absolute immediately (not left as the raw env value / relative default)
# because sqlite3.connect() resolves a relative path against the CURRENT PROCESS's working
# directory at connect time. If `flask init-db` is run from one working directory and the
# app is later started from a different one (a different terminal, an IDE launcher, a
# systemd unit with its own WorkingDirectory, a Docker ENTRYPOINT with a different WORKDIR),
# a relative DB_PATH silently resolves to two different files -- init-db populates one
# (creating it if absent, since SQLite doesn't error on a missing file), and the running app
# opens a fresh, empty one at a different path, surfacing as "no such table: users" with no
# indication that the actual cause is a working-directory mismatch, not a schema bug.
# Resolving to an absolute path once here, using this file's own location as the anchor for
# the relative default, means both commands always agree on the same file regardless of CWD.
DB_PATH = os.path.abspath(
    os.environ.get(
        "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
    )
)
OUTPUT_DIR = os.path.abspath(os.environ.get("OUTPUT_DIR", "/srv/www/example.com/@info"))

# Administrative bootstrap username. Unlike the password below, a predictable default
# username is not a meaningful security weakness on its own (usernames aren't secret), so
# it's fine to keep a static fallback here.
DEPLOYMENT_SUPERADMIN_USER = os.environ.get("SUPERADMIN_USER") or "admin"

# Instantiate and bind the login manager to handle user session lifecycle
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# Define the user model class extending UserMixin for compatibility with Flask-Login
class User(UserMixin):
    # Initialize the user instance with primary ID, username, and password hash
    def __init__(self, id, username, password_hash, password_change_required=False):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.password_change_required = password_change_required


# Register user loader callback function for retrieving session users by database primary key
@login_manager.user_loader
def load_user(user_id):
    # Retrieve user credentials from the database within context safety
    with get_db() as db:
        user_row = db.execute(
            "SELECT id, username, password_hash, password_change_required FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        # Instantiate user object if corresponding row exists in the database
        if user_row:
            return User(
                id=user_row["id"],
                username=user_row["username"],
                password_hash=user_row["password_hash"],
                password_change_required=bool(user_row["password_change_required"]),
            )
    return None


# Context manager wrapper around SQLite database connection lifetime
@contextmanager
def get_db():
    # Establish connection to SQLite database file
    conn = sqlite3.connect(DB_PATH)
    # Enforce foreign key constraints inside SQLite session
    conn.execute("PRAGMA foreign_keys = ON;")
    # Set row factory to dictionary-like access for query outputs
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        # Yield cursor for transaction processing within caller block
        yield cursor
        # Commit modifications if transaction finishes with no exceptions
        conn.commit()
    except Exception:
        # Roll back active transaction state if any exception occurs
        conn.rollback()
        raise
    finally:
        # Guarantee closure of cursor and database connection on exit
        cursor.close()
        conn.close()


# Database initialization routine to set up tables, indexes, and default admin account.
#
# SCHEMA NOTE: buttons are stored as a single JSON TEXT column (site_config 'buttons_json'
# row) rather than as normalized `buttons` / `button_links` / `button_vcards` tables. This
# is a deliberate simplification, not an oversight: every read in this app fetches the
# entire button list at once (get_site_data), and every write replaces the entire button
# list at once (save()). There is no code path that queries, filters, or updates a single
# button in isolation, so the relational schema was paying JOIN/foreign-key overhead for a
# query pattern the app never actually uses. A JSON blob is simpler to reason about here and
# matches the real access pattern. The buttons list is still validated against an explicit
# shape on every write (see validate_buttons_payload) since it now arrives directly from
# client-controlled POST data instead of being assembled field-by-field server-side.
def init_db():
    # DB_PATH is already absolute (resolved at module load, see the DB_PATH assignment
    # above) -- just ensure its parent directory exists.
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as db:
        # Create users table for administrative access control
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_change_required INTEGER NOT NULL DEFAULT 1
            )
            """)

        # Create site configuration table for system values, the buttons JSON blob, and
        # uploaded image blobs (favicon/logo bytes + resolved extension), all keyed by
        # a short string key. See the SCHEMA NOTE above for why buttons live here as JSON
        # rather than in their own relational tables.
        db.execute("""
            CREATE TABLE IF NOT EXISTS site_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                blob_value BLOB
            )
            """)

        # Generate and persist the secret key file if it doesn't already exist. We only
        # write it once -- if the file is already there (e.g. re-running init-db on an
        # existing deployment to reset the password or add a new table), we leave it alone
        # so existing sessions aren't invalidated.
        if not os.path.exists(SECRET_KEY_FILE):
            secret_key = secrets.token_hex(32)  # 256-bit key, hex-encoded
            with open(SECRET_KEY_FILE, "w") as _f:
                _f.write(secret_key)
            os.chmod(SECRET_KEY_FILE, 0o600)  # owner read/write only
            print(f"Generated secret key file: {SECRET_KEY_FILE}")

        # Seed initial administrative user into the database if absent. If no password is
        # provided via SUPERADMIN_PASSWORD, generate a random one and print it once so the
        # operator can log in. We deliberately do NOT fall back to a static hardcoded
        # password: a static default shipped in source is a real credential-stuffing /
        # drive-by risk for anything that ends up reachable (misconfigured VPN, forgotten
        # firewall rule, etc.), whereas a randomly generated one-time password only leaks if
        # the operator fails to read their own deploy logs.
        existing = db.execute(
            "SELECT id FROM users WHERE username = ?", (DEPLOYMENT_SUPERADMIN_USER,)
        ).fetchone()
        if not existing:
            password = os.environ.get("SUPERADMIN_PASSWORD")
            generated = False
            if not password:
                # secrets.token_urlsafe gives a cryptographically random, URL-safe password.
                # 18 bytes -> 24 base64 characters, well above typical brute-force concern
                # for an interactively-typed admin password.
                password = secrets.token_urlsafe(18)
                generated = True

            db.execute(
                "INSERT INTO users (username, password_hash, password_change_required) VALUES (?, ?, 1)",
                (
                    DEPLOYMENT_SUPERADMIN_USER,
                    generate_password_hash(password, method="scrypt"),
                ),
            )

            if generated:
                # Printed once, to stdout/deploy logs only -- never stored in the DB or
                # written to a file on disk. The operator is expected to capture it from
                # the init-db command's output. There is currently no in-app password
                # rotation flow (out of scope for "simple"); to reset, delete the row from
                # the users table and re-run init-db to regenerate it.
                print("=" * 60)
                print(
                    "Generated superadmin password (shown once, not stored anywhere):"
                )
                print(f"  username: {DEPLOYMENT_SUPERADMIN_USER}")
                print(f"  password: {password}")
                print("=" * 60)


# Register CLI command for initializing database and triggering clean initial static bake
@app.cli.command("init-db")
def init_db_command():
    init_db()
    try:
        bake_static_site()
        print("Database schema initialized and initial static bake complete.")
    except Exception as e:
        app.logger.warning(f"Initial bake failed: {e}")


# Helper function to query complete site state from SQLite storage.
DEFAULT_LIGHT_BACKGROUND = "#f8fafc"
DEFAULT_DARK_BACKGROUND = "#0f172a"
ALLOWED_THEMES = {"auto", "light", "dark"}


def validate_hex_color(value: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", (value or "").strip()))


def get_site_data():
    with get_db() as db:
        # Fetch configuration key-value mappings along with binary presence indicators
        config_rows = db.execute(
            "SELECT key, value, blob_value FROM site_config"
        ).fetchall()
        config = {}
        has_favicon = False
        has_org_logo = False

        # Parse key-value results into dictionary structures
        for row in config_rows:
            config[row["key"]] = row["value"]
            if row["key"] == "favicon_blob" and row["blob_value"]:
                has_favicon = True
            if row["key"] == "org_logo_blob" and row["blob_value"]:
                has_org_logo = True

        # Buttons are stored as a JSON-encoded list under the 'buttons_json' key. Fall back
        # to an empty list for a freshly initialized site where nothing has been saved yet.
        raw_buttons_json = config.get("buttons_json") or "[]"
        try:
            buttons = json.loads(raw_buttons_json)
        except (TypeError, ValueError):
            # Defensive fallback: if the stored JSON is ever corrupted, don't crash the
            # entire admin page -- render an empty button list instead so the operator can
            # still log in and fix things.
            app.logger.error(
                "Corrupt buttons_json in site_config; falling back to empty list."
            )
            buttons = []

        # Resolve asset file extensions or apply sensible defaults
        fav_ext = config.get("favicon_ext", ".ico")
        logo_ext = config.get("org_logo_ext", ".png")

        # Return standardized structured data dictionary for view models and renders
        return {
            "title": config.get("title", ""),
            "bio": config.get("bio", ""),
            "theme": (
                config.get("theme", "auto")
                if config.get("theme", "auto") in ALLOWED_THEMES
                else "auto"
            ),
            "background_light": (
                config.get("background_light")
                if validate_hex_color(config.get("background_light", ""))
                else DEFAULT_LIGHT_BACKGROUND
            ),
            "background_dark": (
                config.get("background_dark")
                if validate_hex_color(config.get("background_dark", ""))
                else DEFAULT_DARK_BACKGROUND
            ),
            "has_favicon": has_favicon,
            "has_org_logo": has_org_logo,
            "favicon_filename": f"favicon{fav_ext}",
            "org_logo_filename": f"logo{logo_ext}",
            "buttons": buttons,
        }


# Sanitize slug input to guarantee URL-safe file paths for vCards
def sanitize_slug(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9_\-]", "_", name)
    return name or "contact"


# Schemes we allow a link/vcard URL to resolve to. This is a *scheme* allowlist, not a
# domain allowlist -- it does not restrict which services can be linked to. WhatsApp
# (https://wa.me/...), Telegram (https://t.me/...), LINE (https://line.me/...), Instagram
# (https://instagram.com/...) and similar chat/social apps all use ordinary https:// URLs
# and are completely unaffected by this check. What it blocks is schemes that execute code
# or read local state instead of navigating to a resource: javascript:, data:, vbscript:,
# file:, and anything else not explicitly listed.
ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel", "sms"}


# Validate a vCard website as an absolute HTTP(S) URL. This is intentionally a small
# syntactic check: it does not perform DNS lookups or HTTP requests.
def validate_vcard_website_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return True
    if any(char.isspace() or ord(char) < 32 for char in url):
        return False
    parsed = urlparse(url)
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and bool(parsed.hostname)
    )


# Validate a vCard phone number using libphonenumber's parsing and "possible number"
# validation. We deliberately do not use is_valid_number(): that would impose country-
# specific numbering-plan rules, whereas the admin UI only requires a plausible
# international number. The syntax check remains intentionally narrow so values accepted
# here are exactly "+" followed by digits, spaces, and/or hyphens.
def validate_vcard_phone(phone: str) -> bool:
    phone = (phone or "").strip()
    if not phone:
        return True

    if not re.fullmatch(r"\+[0-9][0-9 -]*", phone):
        return False

    try:
        parsed = phonenumbers.parse(phone, None)
    except NumberParseException:
        return False

    return phonenumbers.is_possible_number(parsed)


# Normalize web URLs to enforce absolute scheme prefixes when missing, then validate the
# resulting scheme against ALLOWED_URL_SCHEMES. Returns "" (dropping the URL entirely) if
# the scheme is not allowed, rather than letting an attacker- or admin-mistake-controlled
# javascript:/data: URI pass straight through into the rendered <a href> on the public page.
def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/") or url.startswith("."):
        # Relative/local paths have no scheme to validate; allow them through unchanged.
        return url
    else:
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"https://{url}"

    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        app.logger.warning(f"Rejected URL with disallowed scheme '{scheme}': {url!r}")
        return ""
    return url


# Escape critical syntax delimiters inside text fields for vCard output format
def clean_vcard_field(val: str) -> str:
    if not val:
        return ""
    val = val.replace("\r", "").replace("\n", " ")
    return val.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


# Format dictionary payload into a RFC 2426 compliant vCard string stream
def generate_vcard_content(vcard: dict) -> str:
    return (
        "BEGIN:VCARD\n"
        "VERSION:3.0\n"
        f"FN:{clean_vcard_field(vcard.get('fn', ''))}\n"
        f"ORG:{clean_vcard_field(vcard.get('org', ''))}\n"
        f"TITLE:{clean_vcard_field(vcard.get('title', ''))}\n"
        f"EMAIL:{clean_vcard_field(vcard.get('email', ''))}\n"
        f"TEL:{clean_vcard_field(vcard.get('phone', ''))}\n"
        f"URL:{clean_vcard_field(vcard.get('url', ''))}\n"
        "END:VCARD\n"
    )


# ---------------------------------------------------------------------------------------
# Image upload validation
# ---------------------------------------------------------------------------------------

# Raster formats we accept for favicon/logo uploads, mapped to the extension we'll write to
# disk. Keyed by Pillow's reported `Image.format` string, which reflects the file's actual
# decoded structure -- not the filename the browser sent us. secure_filename() (used by the
# original code) only protects against path traversal in the filename; it says nothing
# about whether the bytes inside the file are actually an image of the claimed type.
RASTER_FORMAT_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "GIF": ".gif",
    "WEBP": ".webp",
    "ICO": ".ico",
    "BMP": ".bmp",
}

# SVG elements that are never allowed to survive sanitization, regardless of namespace.
# <script> is the direct code-execution vector; <foreignObject> can embed arbitrary
# non-SVG markup (including HTML <script>) inside an SVG document, and <iframe> can load
# an entirely separate origin's content -- both treated as equally disallowed.
SVG_DISALLOWED_TAGS = {"script", "foreignObject", "iframe"}

# Attributes stripped from every surviving SVG element: any event handler (onload, onclick,
# onmouseover, ...) via the prefix check, plus href/xlink:href, which can be used to
# reference and load external/remote content from within the SVG.
SVG_DISALLOWED_ATTR_PREFIXES = ("on",)
SVG_DISALLOWED_ATTRS = {"href", "xlink:href"}


def validate_and_normalize_image(file_storage):
    """
    Validate an uploaded image file by inspecting its actual content rather than trusting
    the client-supplied filename/extension. Returns (bytes, extension) on success, or
    raises ValueError with a user-facing message on failure.

    SVG is handled separately from raster formats because it's XML text, not a binary
    format -- Pillow can't "decode" it to prove safety the way it can a PNG. Instead we
    parse it with defusedxml (which blocks XXE / entity-expansion attacks during parsing
    itself, before we even get to looking at tags) and then strip any element/attribute
    capable of executing script or loading external content before re-serializing it.
    """
    raw_bytes = file_storage.read()
    if not raw_bytes:
        raise ValueError("Uploaded file is empty.")

    # Sniff for SVG first: SVGs are plain text/XML, so raster format detection below
    # wouldn't recognize them at all.
    head = raw_bytes[:512].lstrip().lower()
    looks_like_svg = head.startswith(b"<?xml") or b"<svg" in head

    if looks_like_svg:
        sanitized = sanitize_svg(raw_bytes)
        return sanitized, ".svg"

    # Otherwise, treat it as a raster image and let Pillow verify the structure.
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()  # Raises if the file is truncated/corrupt/not actually an image
        # verify() invalidates the image object for further use, so re-open to read format.
        with Image.open(io.BytesIO(raw_bytes)) as img:
            fmt = img.format
    except UnidentifiedImageError:
        raise ValueError(
            "File is not a recognized image format (PNG/JPEG/GIF/WEBP/ICO/BMP/SVG)."
        )
    except Exception as e:
        raise ValueError(f"Uploaded image failed validation: {e}")

    ext = RASTER_FORMAT_EXTENSIONS.get(fmt)
    if not ext:
        raise ValueError(f"Image format '{fmt}' is not supported.")

    return raw_bytes, ext


def sanitize_svg(raw_bytes):
    """
    Parse an uploaded SVG with defusedxml (blocking XXE/entity-bomb attacks at parse time),
    strip disallowed elements and attributes, and re-serialize. Raises ValueError if the
    input doesn't parse as XML at all.

    This is an allowlist-by-removal approach: rather than trying to enumerate every
    dangerous SVG construct (blocklists for XML/SVG attack surface are routinely
    incomplete -- there have been repeated bypasses found for various SVG sanitizers over
    the years), we remove the small set of constructs that are unambiguously
    script-or-external-load vectors (script/foreignObject/iframe tags, event handler
    attributes, href/xlink:href references) and leave ordinary drawing markup (paths,
    shapes, gradients, etc.) untouched.
    """
    try:
        root = DefusedET.fromstring(raw_bytes)
    except Exception as e:
        raise ValueError(f"File is not valid SVG/XML: {e}")

    def strip_attrs(el):
        # Clean el's OWN attributes. Handles the case where the disallowed attribute is on
        # the element itself (e.g. the root <svg onload="..."> element), not just on some
        # descendant -- a bug in an earlier version of this function only ever checked
        # children's attributes and never the element passed in, which meant a top-level
        # <svg onload="..."> attribute survived sanitization untouched.
        for attr_name in list(el.attrib.keys()):
            local_attr = attr_name.split("}")[-1]
            if (
                local_attr.lower().startswith(SVG_DISALLOWED_ATTR_PREFIXES)
                or local_attr.lower() in SVG_DISALLOWED_ATTRS
            ):
                del el.attrib[attr_name]

    def strip(el):
        strip_attrs(el)
        for child in list(el):
            # child.tag may be namespaced like '{http://www.w3.org/2000/svg}script';
            # compare against the local (post-namespace) name only.
            local_tag = child.tag.split("}")[-1] if isinstance(child.tag, str) else ""
            if local_tag in SVG_DISALLOWED_TAGS:
                el.remove(child)
                continue
            strip(child)

    strip(root)

    # Re-serialize using the standard library's ElementTree -- parsing (the actually
    # dangerous part, vulnerable to XXE) already happened above via defusedxml. Registering
    # the SVG namespace as the default ("") before serializing avoids ElementTree emitting
    # auto-generated ns0: prefixes on every tag (<ns0:svg>, <ns0:circle>, ...) -- purely
    # cosmetic/compatibility, not a security concern, but it keeps the sanitized output
    # closer to ordinary unprefixed SVG that every renderer expects.
    import xml.etree.ElementTree as ET

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="utf-8")


def store_uploaded_image(db, file_field_name, blob_key, ext_key):
    """
    Shared logic for handling the favicon/org_logo upload fields in /save. Validates the
    image, then stores its bytes and resolved extension in site_config. No-op if the field
    is absent or empty (i.e. the operator didn't choose a new file this submission). Raises
    ValueError (caught by the /save route) if validation fails, so the whole /save
    transaction rolls back rather than partially applying.
    """
    if file_field_name not in request.files:
        return
    file = request.files[file_field_name]
    if not file or file.filename == "":
        return

    image_bytes, ext = validate_and_normalize_image(file)

    db.execute(
        "INSERT OR REPLACE INTO site_config (key, value, blob_value) VALUES (?, 'present', ?)",
        (blob_key, sqlite3.Binary(image_bytes)),
    )
    db.execute(
        "INSERT OR REPLACE INTO site_config (key, value) VALUES (?, ?)",
        (ext_key, ext),
    )


# ---------------------------------------------------------------------------------------
# Buttons payload validation (replaces the old parallel-getlist()-array parsing)
# ---------------------------------------------------------------------------------------


def validate_buttons_payload(raw_json: str):
    """
    Parse and validate the 'buttons_json' field submitted by the admin form. Replaces the
    previous approach of reading several parallel form-list arrays (btn_type[], btn_color[],
    link_label[], link_url[], vcard_slug[], ...) and correlating them positionally by index.
    That approach was fragile: if the arrays ever arrived with mismatched lengths or subtly
    out of order (a UI bug, a browser extension reordering fields, a hand-crafted request),
    buttons could get silently corrupted -- e.g. one button's URL attached to a different
    button's label -- with no error raised anywhere.

    Posting a single JSON array instead means each button is a self-contained object, so
    there's no positional correlation to get wrong. We still don't trust it blindly, since
    it's client-controlled data: every button is validated against an explicit shape below,
    and the whole payload is rejected (with a clear error message) if anything doesn't
    match, rather than silently coercing or dropping bad entries.

    Returns (buttons, error). On success error is None. On structural validation failure,
    error is a user-facing string. For vCard field validation failures, error is a dict
    mapping button indexes to invalid field names and messages so the admin form can mark
    individual inputs.
    """
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return None, "Buttons payload was not valid JSON."

    if not isinstance(parsed, list):
        return None, "Buttons payload must be a JSON array."

    validated = []
    seen_slugs = set()
    field_errors = {}

    for i, btn in enumerate(parsed):
        if not isinstance(btn, dict):
            return None, f"Button #{i + 1} is not a JSON object."

        btn_type = btn.get("type")
        color = btn.get("color") or ("#28a745" if btn_type == "link" else "#0284c7")
        text_color = btn.get("text_color") or "#ffffff"
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#28a745" if btn_type == "link" else "#0284c7"
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", text_color):
            text_color = "#ffffff"

        if btn_type == "link":
            label = str(btn.get("label", "")).strip()
            url = str(btn.get("url", "")).strip()
            if not label:
                return None, f"Link button #{i + 1} is missing a label."
            if not url:
                return None, f"Link button #{i + 1} is missing a URL."
            validated.append(
                {
                    "type": "link",
                    "color": color,
                    "text_color": text_color,
                    "label": label,
                    "url": url,
                }
            )

        elif btn_type == "vcard":
            button_label = str(btn.get("button_label", "")).strip() or "Save Contact"
            slug = sanitize_slug(str(btn.get("slug", "")))
            if slug in seen_slugs:
                return None, f"Duplicate vCard slug '{slug}' -- slugs must be unique."
            seen_slugs.add(slug)

            email = str(btn.get("email", "")).strip()
            phone = str(btn.get("phone", "")).strip()
            website_url = str(btn.get("url", "")).strip()
            errors = {}

            if email:
                try:
                    # Syntax validation only; do not perform deliverability/DNS checks.
                    validate_email(email, check_deliverability=False)
                except EmailNotValidError:
                    errors["email"] = "Enter a valid email address."

            if not validate_vcard_phone(phone):
                errors["phone"] = (
                    "Enter a phone number starting with + and containing only numbers, spaces, or hyphens."
                )

            if not validate_vcard_website_url(website_url):
                errors["url"] = "Enter a valid HTTP(S) website URL."

            if errors:
                field_errors[i] = errors

            validated.append(
                {
                    "type": "vcard",
                    "color": color,
                    "text_color": text_color,
                    "button_label": button_label,
                    "slug": slug,
                    "fn": str(btn.get("fn", "")).strip(),
                    "org": str(btn.get("org", "")).strip(),
                    "title": str(btn.get("title", "")).strip(),
                    "email": email,
                    "phone": phone,
                    "url": website_url,
                }
            )

        else:
            return None, f"Button #{i + 1} has an unrecognized type: {btn_type!r}"

    if field_errors:
        return None, field_errors

    return validated, None


# ---------------------------------------------------------------------------------------
# Static site generation
# ---------------------------------------------------------------------------------------


# Clear everything INSIDE the output directory, without removing the directory itself.
# OUTPUT_DIR is likely a Docker bind-mount or named-volume mount point in deployment (per
# the docstring's separation of admin app vs. static-site hosting). shutil.rmtree(directory)
# would delete that mount point's inode and require os.makedirs to recreate it -- inside a
# container, the recreated directory is just a fresh directory in the container's own
# filesystem, no longer the mounted host path, which either breaks the mount or only
# "works" by accident depending on the container runtime's bind-mount semantics. Removing
# and recreating each entry INSIDE the directory instead leaves the mount point itself
# untouched, which is safe under both plain-filesystem and mounted-volume deployments.
def clean_output_directory(directory: str):
    os.makedirs(directory, exist_ok=True)
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.remove(entry.path)


# Core site generation routine: clears the output directory and writes a fresh static site.
def bake_static_site():
    clean_output_directory(OUTPUT_DIR)

    favicon_filename = None
    logo_filename = None

    with get_db() as db:
        # Export binary favicon from site config if present in database
        favicon_row = db.execute(
            "SELECT blob_value FROM site_config WHERE key = 'favicon_blob'"
        ).fetchone()

        if favicon_row and favicon_row["blob_value"]:
            ext_row = db.execute(
                "SELECT value FROM site_config WHERE key = 'favicon_ext'"
            ).fetchone()
            ext = ext_row["value"] if ext_row and ext_row["value"] else ".ico"
            favicon_filename = f"favicon{ext}"
            with open(os.path.join(OUTPUT_DIR, favicon_filename), "wb") as f_out:
                f_out.write(favicon_row["blob_value"])

        # Export binary logo image from site config if present in database
        logo_row = db.execute(
            "SELECT blob_value FROM site_config WHERE key = 'org_logo_blob'"
        ).fetchone()

        if logo_row and logo_row["blob_value"]:
            ext_row = db.execute(
                "SELECT value FROM site_config WHERE key = 'org_logo_ext'"
            ).fetchone()
            ext = ext_row["value"] if ext_row and ext_row["value"] else ".png"
            logo_filename = f"logo{ext}"
            with open(os.path.join(OUTPUT_DIR, logo_filename), "wb") as f_out:
                f_out.write(logo_row["blob_value"])

    # Retrieve current structured state from SQLite backend
    data = get_site_data()

    processed_buttons = []
    # Process configuration buttons and compile individual vCard files
    for btn in data.get("buttons", []):
        if btn["type"] == "link":
            processed_buttons.append(
                {
                    "type": "link",
                    "color": btn.get("color", "#28a745"),
                    "text_color": btn.get("text_color", "#ffffff"),
                    "label": btn["label"],
                    "target_url": normalize_url(btn["url"]),
                }
            )
        elif btn["type"] == "vcard":
            slug = sanitize_slug(btn.get("slug", "contact"))
            vcard_filename = f"{slug}.vcf"
            vcard_path = os.path.join(OUTPUT_DIR, vcard_filename)

            vcard_data = {
                "fn": btn.get("fn", ""),
                "org": btn.get("org", ""),
                "title": btn.get("title", ""),
                "email": btn.get("email", ""),
                "phone": btn.get("phone", ""),
                "url": normalize_url(btn.get("url", "")),
            }

            vcard_content = generate_vcard_content(vcard_data)
            with open(vcard_path, "w", encoding="utf-8") as f_out:
                f_out.write(vcard_content)

            processed_buttons.append(
                {
                    "type": "vcard",
                    "color": btn.get("color", "#0284c7"),
                    "text_color": btn.get("text_color", "#ffffff"),
                    "label": btn.get("button_label", "Save Contact"),
                    "target_url": f"./{vcard_filename}",
                }
            )

    # Compile context object for Jinja2 template engine execution
    render_context = {
        "title": data["title"],
        "bio": data["bio"],
        "theme": data.get("theme", "auto"),
        "background_light": data.get("background_light", DEFAULT_LIGHT_BACKGROUND),
        "background_dark": data.get("background_dark", DEFAULT_DARK_BACKGROUND),
        "favicon_filename": favicon_filename,
        "org_logo_filename": logo_filename,
        "buttons": processed_buttons,
    }

    # Render Jinja template into output HTML string and write index file
    rendered_html = render_template("site_template.jinja2", data=render_context)
    index_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f_out:
        f_out.write(rendered_html)


# ---------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------


# Handle login form displays and authentication post requests. This form's CSRF token is
# enforced by CSRFProtect like every other mutating route in the app -- see login.jinja2
# for the hidden {{ csrf_token() }} field. (CSRF on a login form primarily protects against
# login-CSRF -- an attacker forcing a victim's browser to authenticate as an
# attacker-controlled account -- rather than session hijacking, but it's cheap to include
# uniformly rather than special-casing "this POST route doesn't need it.")
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        if current_user.password_change_required:
            return redirect(url_for("change_password"))
        return redirect(url_for("admin"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_db() as db:
            user_row = db.execute(
                "SELECT id, username, password_hash, password_change_required FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        # Verify hashed password string against user submission
        if user_row and check_password_hash(user_row["password_hash"], password):
            user = User(
                id=user_row["id"],
                username=user_row["username"],
                password_hash=user_row["password_hash"],
                password_change_required=bool(user_row["password_change_required"]),
            )
            login_user(user)
            if user.password_change_required:
                return redirect(url_for("change_password"))
            return redirect(url_for("admin"))

        flash("Invalid username or password.", "danger")

    return render_template("login.jinja2")


# Handle explicit user logout requests
@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# Administrative dashboard GET interface route
@app.route("/", methods=["GET"])
@login_required
def admin():
    if current_user.password_change_required:
        return redirect(url_for("change_password"))
    data = get_site_data()
    return render_template("admin.jinja2", data=data, user=current_user)


# Handle password changes for the currently authenticated administrator.
# The current password is required before the new password can be accepted, and the new
# password is hashed with the same scrypt-based Werkzeug helper used during initialization.
@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(current_user.password_hash, current_password):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("change_password"))

        if not new_password:
            flash("New password cannot be empty.", "danger")
            return redirect(url_for("change_password"))

        if new_password != confirm_password:
            flash("New passwords do not match.", "danger")
            return redirect(url_for("change_password"))

        if new_password == current_password:
            flash("New password must be different from the current password.", "danger")
            return redirect(url_for("change_password"))

        new_password_hash = generate_password_hash(new_password, method="scrypt")

        try:
            with get_db() as db:
                db.execute(
                    "UPDATE users SET password_hash = ?, password_change_required = 0 WHERE id = ?",
                    (new_password_hash, current_user.id),
                )
        except Exception as e:
            app.logger.error(f"Database error during password change: {e}")
            flash(
                "A database error occurred while changing the password. Check the server log for details.",
                "danger",
            )
            return redirect(url_for("change_password"))

        current_user.password_hash = new_password_hash
        current_user.password_change_required = False
        flash("Password changed successfully.", "success")
        return redirect(url_for("admin"))

    return render_template("change_password.jinja2")


# Handle configuration modifications, file uploads, and trigger dynamic bakes.
#
# Buttons now arrive as a single JSON-encoded field ('buttons_json') built client-side by
# admin.jinja2's JS from the button-block DOM, rather than as several parallel form-list
# arrays correlated by index. See validate_buttons_payload() for why. CSRF-protected like
# every mutating route here, via the {{ csrf_token() }} hidden field in admin.jinja2's form.
@app.route("/save", methods=["POST"])
@login_required
def save():
    if current_user.password_change_required:
        return redirect(url_for("change_password"))

    title = request.form.get("site_title", "")
    bio = request.form.get("bio", "")
    theme = request.form.get("theme", "auto")
    background_light = request.form.get(
        "background_light", DEFAULT_LIGHT_BACKGROUND
    ).strip()
    background_dark = request.form.get(
        "background_dark", DEFAULT_DARK_BACKGROUND
    ).strip()
    raw_buttons_json = request.form.get("buttons_json", "[]")

    parsed_buttons, error = validate_buttons_payload(raw_buttons_json)

    # Preserve the submitted button objects whenever the JSON itself is parseable, including
    # when individual vCard fields are invalid. This lets the form mark the offending fields
    # without losing the rest of the in-progress edits.
    try:
        posted_buttons_for_render = json.loads(raw_buttons_json)
    except (TypeError, ValueError):
        posted_buttons_for_render = []

    posted_state = {
        "title": title,
        "bio": bio,
        "theme": theme,
        "background_light": background_light,
        "background_dark": background_dark,
        "buttons": posted_buttons_for_render,
    }

    if isinstance(error, dict):
        return (
            render_template(
                "admin.jinja2",
                data=posted_state,
                user=current_user,
                validation_errors=error,
            ),
            400,
        )

    if theme not in ALLOWED_THEMES:
        flash("Error: Invalid page theme.", "danger")
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            400,
        )

    if not validate_hex_color(background_light) or not validate_hex_color(
        background_dark
    ):
        flash(
            "Error: Page background colors must be six-digit hexadecimal colors.",
            "danger",
        )
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            400,
        )

    if error:
        flash(f"Error: {error}", "danger")
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            400,
        )

    try:
        # Atomic database transaction block spanning config, image, and button updates.
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO site_config (key, value) VALUES ('title', ?)",
                (title,),
            )
            db.execute(
                "INSERT OR REPLACE INTO site_config (key, value) VALUES ('bio', ?)",
                (bio,),
            )
            db.execute(
                "INSERT OR REPLACE INTO site_config (key, value) VALUES ('theme', ?)",
                (theme,),
            )
            db.execute(
                "INSERT OR REPLACE INTO site_config (key, value) VALUES ('background_light', ?)",
                (background_light,),
            )
            db.execute(
                "INSERT OR REPLACE INTO site_config (key, value) VALUES ('background_dark', ?)",
                (background_dark,),
            )
            db.execute(
                "INSERT OR REPLACE INTO site_config (key, value) VALUES ('buttons_json', ?)",
                (json.dumps(parsed_buttons),),
            )

            # store_uploaded_image validates image content (magic bytes for raster formats,
            # parsed + sanitized for SVG) before writing anything -- see its docstring. A
            # ValueError here aborts the whole transaction (get_db's except-block rolls
            # back everything above too), so a bad image upload never partially applies
            # alongside other changes.
            store_uploaded_image(db, "favicon", "favicon_blob", "favicon_ext")
            store_uploaded_image(db, "org_logo", "org_logo_blob", "org_logo_ext")

    except ValueError as e:
        # Raised by store_uploaded_image on invalid/unrecognized image content.
        flash(f"Image upload error: {e}", "danger")
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            400,
        )
    except Exception as e:
        # Log the real exception server-side; show the admin a generic message rather than
        # raw exception text (which could include SQL fragments or internal paths).
        app.logger.error(f"Database error during save: {e}")
        flash(
            "A database error occurred while saving. Check the server log for details.",
            "danger",
        )
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            500,
        )

    # Execute static bake step to generate site files on disk
    try:
        bake_static_site()
        flash("Settings saved and static site baked successfully!", "success")
    except Exception as e:
        app.logger.error(f"Error baking static site: {e}")
        flash(
            "Settings were saved, but baking the static site failed. Check the server log.",
            "danger",
        )

    return redirect(url_for("admin"))


# Run development server if executed directly as entrypoint script
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

import os
import json
import sqlite3
import io
import shutil
from contextlib import contextmanager
from urllib.parse import urlparse
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, session
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
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from email_validator import EmailNotValidError, validate_email
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException
from PIL import Image, UnidentifiedImageError
from slugify import slugify

from config import (
    DB_PATH,
    SECRET_KEY_FILE,
    ALLOWED_THEMES,
    DEFAULT_LIGHT_BG,
    DEFAULT_DARK_BG,
    ICON_DIR,
    OUTPUT_DIR,
    HEX_COLOR_RE,
    RASTER_FORMAT_EXTENSIONS,
)

# -----------------------------------------------------------------------------
# App Factory & Config
# -----------------------------------------------------------------------------
# We use Flask's standard 'static' folder. Icons live in static/icons/.
# This allows us to serve them efficiently via Flask's built-in static file handling.
app = Flask(__name__, static_folder="static", static_url_path="/static")

# Load the secret key. We deliberately do NOT fall back to os.urandom() here.
# See config.py for the explanation of why silent-random-fallbacks are dangerous
# in multi-worker environments.
if os.environ.get("FLASK_SECRET_KEY"):
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
elif os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, "rb") as _f:
        app.secret_key = _f.read().strip()
else:
    app.secret_key = None


@app.before_request
def enforce_secret_key():
    # Raise here rather than at module level so `init-db` can still run to generate
    # the key file. Once the key is present, this hook reads it and installs it.
    if app.secret_key is None:
        if os.path.exists(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, "rb") as _f:
                app.secret_key = _f.read().strip()
        else:
            raise RuntimeError(
                f"No secret key found. Run `python init-db.py` to generate one. "
                f"Expected key file: {SECRET_KEY_FILE!r}"
            )


# Apply proxy fix middleware to handle HTTP headers correctly behind reverse proxies like Nginx
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Evaluate security boolean from environment variables for cookie handling
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")

# Apply Flask configuration parameters for session security and file size limits.
app.config.update(
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

# Enable CSRF protection for every state-changing view in this app.
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    logout_user()
    session.clear()
    flash("Your session has expired. Please log in again.", "danger")
    return redirect(url_for("login"))


# Instantiate and bind the login manager to handle user session lifecycle
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, id, username, password_hash, password_change_required=False):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.password_change_required = password_change_required


@login_manager.user_loader
def load_user(user_id):
    with get_db() as db:
        row = db.execute(
            "SELECT id, username, password_hash, password_change_required FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return User(
                row["id"],
                row["username"],
                row["password_hash"],
                bool(row["password_change_required"]),
            )
    return None


# -----------------------------------------------------------------------------
# DB Helper (Local to app.py)
# -----------------------------------------------------------------------------
@contextmanager
def get_db():
    """Context manager wrapper around SQLite database connection lifetime."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# -----------------------------------------------------------------------------
# Validation Helpers
# -----------------------------------------------------------------------------
# Schemes we allow a link/vcard URL to resolve to. This blocks javascript:, data:, etc.
ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel", "sms"}


def normalize_url(url: str) -> str:
    """Normalize web URLs to enforce absolute scheme prefixes when missing."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/") or url.startswith("."):
        return url
    else:
        parsed = urlparse(url)
        if not parsed.scheme:
            url = f"https://{url}"
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        return ""
    return url


def validate_vcard_phone(phone: str) -> bool:
    """Validate a vCard phone number using libphonenumber's 'possible number' check."""
    phone = (phone or "").strip()
    if not phone:
        return True
    import re

    if not re.fullmatch(r"\+[0-9][0-9 -]*", phone):
        return False
    try:
        parsed = phonenumbers.parse(phone, None)
    except NumberParseException:
        return False
    return phonenumbers.is_possible_number(parsed)


def validate_vcard_website_url(url: str) -> bool:
    """Validate a vCard website as an absolute HTTP(S) URL."""
    url = (url or "").strip()
    if not url:
        return True
    if any(char.isspace() or ord(char) < 32 for char in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def clean_vcard_field(val: str) -> str:
    """Escape critical syntax delimiters inside text fields for vCard output format."""
    if not val:
        return ""
    val = val.replace("\r", "").replace("\n", " ")
    return val.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def generate_vcard_content(vcard: dict) -> str:
    """Format dictionary payload into a RFC 2426 compliant vCard string stream."""
    return (
        "BEGIN:VCARD\nVERSION:3.0\n"
        f"FN:{clean_vcard_field(vcard.get('fn', ''))}\n"
        f"ORG:{clean_vcard_field(vcard.get('org', ''))}\n"
        f"TITLE:{clean_vcard_field(vcard.get('title', ''))}\n"
        f"EMAIL:{clean_vcard_field(vcard.get('email', ''))}\n"
        f"TEL:{clean_vcard_field(vcard.get('phone', ''))}\n"
        f"URL:{clean_vcard_field(vcard.get('url', ''))}\nEND:VCARD\n"
    )


def validate_link_button(btn, index, valid_icons):
    """Validate a single 'link' type button."""
    label = str(btn.get("label", "")).strip()
    url = str(btn.get("url", "")).strip()
    if not label:
        return None, f"Link button #{index+1} is missing a label."
    if not url:
        return None, f"Link button #{index+1} is missing a URL."

    color = btn.get("color") or "#28a745"
    text_color = btn.get("text_color") or "#ffffff"
    if not HEX_COLOR_RE.fullmatch(color):
        color = "#28a745"
    if not HEX_COLOR_RE.fullmatch(text_color):
        text_color = "#ffffff"

    icon = str(btn.get("icon", "")).strip()
    if icon and icon not in valid_icons:
        return None, f"Button #{index+1} uses an unknown brand icon: {icon!r}."

    return {
        "type": "link",
        "color": color,
        "text_color": text_color,
        "label": label,
        "url": url,
        "icon": icon,
    }, None


def validate_vcard_button(btn, index, seen_slugs, valid_icons):
    """Validate a single 'vcard' type button."""
    button_label = str(btn.get("button_label", "")).strip() or "Save Contact"
    slug = slugify(str(btn.get("slug", "")), lowercase=True) or "contact"
    if slug in seen_slugs:
        return None, f"Duplicate vCard slug '{slug}' -- slugs must be unique."

    email = str(btn.get("email", "")).strip()
    phone = str(btn.get("phone", "")).strip()
    website_url = str(btn.get("url", "")).strip()
    errors = {}

    if email:
        try:
            validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            errors["email"] = "Enter a valid email address."
    if not validate_vcard_phone(phone):
        errors["phone"] = "Enter a phone number starting with +."
    if not validate_vcard_website_url(website_url):
        errors["url"] = "Enter a valid HTTP(S) website URL."

    if errors:
        return None, errors

    color = btn.get("color") or "#0284c7"
    text_color = btn.get("text_color") or "#ffffff"
    if not HEX_COLOR_RE.fullmatch(color):
        color = "#0284c7"
    if not HEX_COLOR_RE.fullmatch(text_color):
        text_color = "#ffffff"

    icon = str(btn.get("icon", "")).strip()
    if icon and icon not in valid_icons:
        return None, f"Button #{index+1} uses an unknown brand icon: {icon!r}."

    return {
        "type": "vcard",
        "color": color,
        "text_color": text_color,
        "button_label": button_label,
        "icon": icon,
        "slug": slug,
        "fn": str(btn.get("fn", "")).strip(),
        "org": str(btn.get("org", "")).strip(),
        "title": str(btn.get("title", "")).strip(),
        "email": email,
        "phone": phone,
        "url": website_url,
    }, None


def validate_buttons_payload(raw_json: str):
    """
    Parse and validate the 'buttons_json' field submitted by the admin form.
    Replaces the previous approach of reading several parallel form-list arrays.
    Posting a single JSON array means each button is a self-contained object.
    """
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return None, "Buttons payload was not valid JSON."
    if not isinstance(parsed, list):
        return None, "Buttons payload must be a JSON array."

    valid_icons = set(
        os.path.splitext(os.path.basename(p))[0]
        for p in Path(ICON_DIR).glob("*.svg", case_sensitive=False)
    )
    validated = []
    seen_slugs = set()
    field_errors = {}

    for i, btn in enumerate(parsed):
        if not isinstance(btn, dict):
            return None, f"Button #{i+1} is not a JSON object."
        btn_type = btn.get("type")

        if btn_type == "link":
            v_btn, error = validate_link_button(btn, i, valid_icons)
        elif btn_type == "vcard":
            v_btn, error = validate_vcard_button(btn, i, seen_slugs, valid_icons)
            if not error:
                seen_slugs.add(v_btn["slug"])
        else:
            return None, f"Button #{i+1} has an unrecognized type: {btn_type!r}"

        if error:
            if isinstance(error, dict):
                field_errors[i] = error
            else:
                return None, error
        else:
            validated.append(v_btn)

    if field_errors:
        return None, field_errors
    return validated, None


# -----------------------------------------------------------------------------
# Static Site Baking
# -----------------------------------------------------------------------------
def bake_static_site():
    """Core site generation routine: clears the output directory and writes a fresh static site."""
    # Clear everything INSIDE the output directory, without removing the directory itself.
    # This preserves Docker bind-mounts.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for entry in os.scandir(OUTPUT_DIR):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.remove(entry.path)

    favicon_filename = logo_filename = None
    with get_db() as db:
        # Export binary favicon from site config if present in database
        fav_row = db.execute(
            "SELECT blob_value FROM site_config WHERE key='favicon_blob'"
        ).fetchone()
        if fav_row and fav_row["blob_value"]:
            ext = (
                db.execute(
                    "SELECT value FROM site_config WHERE key='favicon_ext'"
                ).fetchone()["value"]
                or ".ico"
            )
            favicon_filename = f"favicon{ext}"
            with open(os.path.join(OUTPUT_DIR, favicon_filename), "wb") as f:
                f.write(fav_row["blob_value"])

        # Export binary logo image from site config if present in database
        logo_row = db.execute(
            "SELECT blob_value FROM site_config WHERE key='org_logo_blob'"
        ).fetchone()
        if logo_row and logo_row["blob_value"]:
            ext = (
                db.execute(
                    "SELECT value FROM site_config WHERE key='org_logo_ext'"
                ).fetchone()["value"]
                or ".png"
            )
            logo_filename = f"logo{ext}"
            with open(os.path.join(OUTPUT_DIR, logo_filename), "wb") as f:
                f.write(logo_row["blob_value"])

        # Retrieve current structured state from SQLite backend
        config_rows = db.execute("SELECT key, value FROM site_config").fetchall()
        config = {r["key"]: r["value"] for r in config_rows}
        buttons = json.loads(config.get("buttons_json", "[]"))

    # Copy selected icons from static/icons/ to the public output directory
    os.makedirs(os.path.join(OUTPUT_DIR, "icons"), exist_ok=True)
    for btn in buttons:
        icon_name = btn.get("icon")
        if icon_name:
            src = os.path.join(ICON_DIR, f"{icon_name}.svg")
            dst = os.path.join(OUTPUT_DIR, "icons", f"{icon_name}.svg")
            if os.path.exists(src):
                shutil.copy2(src, dst)

    processed_buttons = []
    for btn in buttons:
        if btn["type"] == "link":
            processed_buttons.append(
                {
                    "type": "link",
                    "color": btn.get("color", "#28a745"),
                    "text_color": btn.get("text_color", "#ffffff"),
                    "label": btn["label"],
                    "icon_filename": (
                        f"./icons/{btn['icon']}.svg" if btn.get("icon") else None
                    ),
                    "target_url": normalize_url(btn["url"]),
                }
            )
        elif btn["type"] == "vcard":
            slug = btn.get("slug", "contact")
            vcard_content = generate_vcard_content(
                {k: btn.get(k, "") for k in ["fn", "org", "title", "email", "phone"]}
            )
            vcard_content = vcard_content.replace(
                "URL:\n", f"URL:{normalize_url(btn.get('url',''))}\n"
            )
            with open(
                os.path.join(OUTPUT_DIR, f"{slug}.vcf"), "w", encoding="utf-8"
            ) as f:
                f.write(vcard_content)
            processed_buttons.append(
                {
                    "type": "vcard",
                    "color": btn.get("color", "#0284c7"),
                    "text_color": btn.get("text_color", "#ffffff"),
                    "label": btn.get("button_label", "Save Contact"),
                    "icon_filename": (
                        f"./icons/{btn['icon']}.svg" if btn.get("icon") else None
                    ),
                    "target_url": f"./{slug}.vcf",
                }
            )

    # Compile context object for Jinja2 template engine execution
    ctx = {
        "title": config.get("title", ""),
        "bio": config.get("bio", ""),
        "theme": (
            config.get("theme", "auto")
            if config.get("theme", "auto") in ALLOWED_THEMES
            else "auto"
        ),
        "background_light": (
            config.get("background_light")
            if HEX_COLOR_RE.match(config.get("background_light", "") or "")
            else DEFAULT_LIGHT_BG
        ),
        "background_dark": (
            config.get("background_dark")
            if HEX_COLOR_RE.match(config.get("background_dark", "") or "")
            else DEFAULT_DARK_BG
        ),
        "favicon_filename": favicon_filename,
        "org_logo_filename": logo_filename,
        "buttons": processed_buttons,
    }

    # Render Jinja template into output HTML string and write index file
    html = render_template("site_template.jinja2", data=ctx)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def get_icon_catalog_metadata():
    """Load the pre-compiled custom catalog from the database and format for UI."""
    icon_files = list(Path(ICON_DIR).glob("*.svg", case_sensitive=False))
    if icon_files:
        icon_ids = sorted(os.path.splitext(os.path.basename(f))[0] for f in icon_files)
        return [{"id": i, "name": i.replace("-", " ").title()} for i in icon_ids]
    return []


def store_uploaded_image(db, file_field_name, blob_key, ext_key):
    """
    Shared logic for handling the favicon/org_logo upload fields in /save.
    Validates the image, then stores its bytes and resolved extension in site_config.
    """
    if file_field_name not in request.files:
        return
    file = request.files[file_field_name]
    if not file or file.filename == "":
        return

    raw_bytes = file.read()
    if not raw_bytes:
        raise ValueError("Uploaded file is empty.")

    # Check if it's an SVG
    head = raw_bytes[:512].lstrip().lower()
    is_svg = head.startswith(b"<?xml") or b"<svg" in head

    if is_svg:
        # For SVG, just validate it's well-formed XML and store it
        try:
            import xml.etree.ElementTree as ET

            ET.fromstring(raw_bytes)
        except ET.ParseError:
            raise ValueError("File is not a valid SVG/XML file.")
        ext = ".svg"
    else:
        # Otherwise, treat it as a raster image and let Pillow verify the structure.
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                img.verify()
            with Image.open(io.BytesIO(raw_bytes)) as img:
                fmt = img.format
        except UnidentifiedImageError:
            raise ValueError("File is not a recognized image format.")

        ext = RASTER_FORMAT_EXTENSIONS.get(fmt)
        if not ext:
            raise ValueError(f"Image format '{fmt}' is not supported.")

    db.execute(
        "INSERT OR REPLACE INTO site_config (key, value, blob_value) VALUES (?, 'present', ?)",
        (blob_key, sqlite3.Binary(raw_bytes)),
    )
    db.execute(
        "INSERT OR REPLACE INTO site_config (key, value) VALUES (?, ?)", (ext_key, ext)
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    """Handle login form displays and authentication post requests."""
    if current_user.is_authenticated:
        if current_user.password_change_required:
            return redirect(url_for("change_password"))
        return redirect(url_for("admin"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_db() as db:
            row = db.execute(
                "SELECT id, username, password_hash, password_change_required FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if row and check_password_hash(row["password_hash"], password):
            user = User(
                row["id"],
                row["username"],
                row["password_hash"],
                bool(row["password_change_required"]),
            )
            login_user(user)
            if user.password_change_required:
                return redirect(url_for("change_password"))
            return redirect(url_for("admin"))

        flash("Invalid username or password.", "danger")

    return render_template("login.jinja2")


@app.route("/logout")
@login_required
def logout():
    """Handle explicit user logout requests."""
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def admin():
    """Administrative dashboard GET interface route."""
    if current_user.password_change_required:
        return redirect(url_for("change_password"))

    # Load site data manually for admin view
    with get_db() as db:
        config_rows = db.execute("SELECT key, value FROM site_config").fetchall()
        config = {r["key"]: r["value"] for r in config_rows}
        buttons = json.loads(config.get("buttons_json", "[]"))
        data = {
            "title": config.get("title", ""),
            "bio": config.get("bio", ""),
            "theme": config.get("theme", "auto"),
            "background_light": config.get("background_light", DEFAULT_LIGHT_BG),
            "background_dark": config.get("background_dark", DEFAULT_DARK_BG),
            "buttons": buttons,
        }

    icon_catalog = get_icon_catalog_metadata()
    return render_template(
        "admin.jinja2", data=data, user=current_user, icon_catalog=icon_catalog
    )


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Handle password changes for the currently authenticated administrator."""
    if request.method == "POST":
        cur = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not check_password_hash(current_user.password_hash, cur):
            flash("Current password is incorrect.", "danger")
        elif not new:
            flash("New password cannot be empty.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        elif new == cur:
            flash("New password must be different from the current password.", "danger")
        else:
            h = generate_password_hash(new, method="scrypt")
            with get_db() as db:
                db.execute(
                    "UPDATE users SET password_hash=?, password_change_required=0 WHERE id=?",
                    (h, current_user.id),
                )
            current_user.password_hash = h
            current_user.password_change_required = False
            flash("Password changed successfully.", "success")
            return redirect(url_for("admin"))
    return render_template("change_password.jinja2")


@app.route("/save", methods=["POST"])
@login_required
def save():
    """Handle configuration modifications, file uploads, and trigger dynamic bakes."""
    if current_user.password_change_required:
        return redirect(url_for("change_password"))

    title = request.form.get("site_title", "")
    bio = request.form.get("bio", "")
    theme = request.form.get("theme", "auto")
    bg_l = request.form.get("background_light", DEFAULT_LIGHT_BG).strip()
    bg_d = request.form.get("background_dark", DEFAULT_DARK_BG).strip()
    raw_btns = request.form.get("buttons_json", "[]")

    parsed_buttons, error = validate_buttons_payload(raw_btns)
    try:
        posted_btns = json.loads(raw_btns)
    except:
        posted_btns = []

    state = {
        "title": title,
        "bio": bio,
        "theme": theme,
        "background_light": bg_l,
        "background_dark": bg_d,
        "buttons": posted_btns,
    }

    if isinstance(error, dict):
        return (
            render_template(
                "admin.jinja2", data=state, user=current_user, validation_errors=error
            ),
            400,
        )
    if theme not in ALLOWED_THEMES:
        flash("Error: Invalid page theme.", "danger")
        return render_template("admin.jinja2", data=state, user=current_user), 400
    if not HEX_COLOR_RE.match(bg_l) or not HEX_COLOR_RE.match(bg_d):
        flash(
            "Error: Page background colors must be six-digit hexadecimal colors.",
            "danger",
        )
        return render_template("admin.jinja2", data=state, user=current_user), 400
    if error:
        flash(f"Error: {error}", "danger")
        return render_template("admin.jinja2", data=state, user=current_user), 400

    try:
        with get_db() as db:
            for k, v in [
                ("title", title),
                ("bio", bio),
                ("theme", theme),
                ("background_light", bg_l),
                ("background_dark", bg_d),
                ("buttons_json", json.dumps(parsed_buttons)),
            ]:
                db.execute(
                    "INSERT OR REPLACE INTO site_config (key,value) VALUES (?,?)",
                    (k, v),
                )
            store_uploaded_image(db, "favicon", "favicon_blob", "favicon_ext")
            store_uploaded_image(db, "org_logo", "org_logo_blob", "org_logo_ext")
    except ValueError as e:
        flash(f"Image upload error: {e}", "danger")
        return render_template("admin.jinja2", data=state, user=current_user), 400
    except Exception as e:
        app.logger.error(f"Database error during save: {e}")
        flash("A database error occurred while saving.", "danger")
        return render_template("admin.jinja2", data=state, user=current_user), 500

    try:
        bake_static_site()
        flash("Settings saved and static site baked successfully!", "success")
    except Exception as e:
        app.logger.error(f"Error baking static site: {e}")
        flash("Settings were saved, but baking the static site failed.", "danger")

    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

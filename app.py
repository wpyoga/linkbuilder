import os
import json
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    Response,
)
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
from urllib.parse import urlparse

from common import (
    get_db,
    load_icon_catalog,
    validate_and_normalize_image,
    get_site_data,
    validate_hex_color,
    sanitize_svg,
    SECRET_KEY_FILE,
    ALLOWED_THEMES,
    DEFAULT_LIGHT_BACKGROUND,
    DEFAULT_DARK_BACKGROUND,
    ICON_NAME_RE,
    OUTPUT_DIR,
)

# -----------------------------------------------------------------------------
# App Factory & Config
# -----------------------------------------------------------------------------
app = Flask(__name__)

# Secret Key Loading
if os.environ.get("FLASK_SECRET_KEY"):
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
elif os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, "rb") as _f:
        app.secret_key = _f.read().strip()
else:
    app.secret_key = None


@app.before_request
def enforce_secret_key():
    if app.secret_key is None:
        if os.path.exists(SECRET_KEY_FILE):
            with open(SECRET_KEY_FILE, "rb") as _f:
                app.secret_key = _f.read().strip()
        else:
            raise RuntimeError(
                f"No secret key found. Run `python init-db.py` to generate one. "
                f"Expected: {SECRET_KEY_FILE!r}"
            )


app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")
app.config.update(
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    logout_user()
    session.clear()
    flash("Your session has expired. Please log in again.", "danger")
    return redirect(url_for("login"))


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
# Validation Helpers (Route-Specific)
# -----------------------------------------------------------------------------
ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel", "sms"}


def sanitize_slug(name: str) -> str:
    name = (name or "").strip().lower()
    import re

    name = re.sub(r"[^a-z0-9_\-]", "_", name)
    return name or "contact"


def normalize_url(url: str) -> str:
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
    url = (url or "").strip()
    if not url:
        return True
    if any(char.isspace() or ord(char) < 32 for char in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def clean_vcard_field(val: str) -> str:
    if not val:
        return ""
    val = val.replace("\r", "").replace("\n", " ")
    return val.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def generate_vcard_content(vcard: dict) -> str:
    return (
        "BEGIN:VCARD\nVERSION:3.0\n"
        f"FN:{clean_vcard_field(vcard.get('fn', ''))}\n"
        f"ORG:{clean_vcard_field(vcard.get('org', ''))}\n"
        f"TITLE:{clean_vcard_field(vcard.get('title', ''))}\n"
        f"EMAIL:{clean_vcard_field(vcard.get('email', ''))}\n"
        f"TEL:{clean_vcard_field(vcard.get('phone', ''))}\n"
        f"URL:{clean_vcard_field(vcard.get('url', ''))}\nEND:VCARD\n"
    )


def validate_buttons_payload(raw_json: str):
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return None, "Buttons payload was not valid JSON."
    if not isinstance(parsed, list):
        return None, "Buttons payload must be a JSON array."

    validated = []
    seen_slugs = set()
    field_errors = {}
    catalog = None
    try:
        catalog = load_icon_catalog()
    except Exception:
        pass

    for i, btn in enumerate(parsed):
        if not isinstance(btn, dict):
            return None, f"Button #{i+1} is not a JSON object."
        btn_type = btn.get("type")
        color = btn.get("color") or ("#28a745" if btn_type == "link" else "#0284c7")
        icon = str(btn.get("icon", "")).strip()
        if icon:
            if not ICON_NAME_RE.fullmatch(icon):
                return None, f"Button #{i+1} invalid icon ID."
            if catalog and icon not in catalog["icons"]:
                return None, f"Button #{i+1} unknown icon: {icon!r}"

        text_color = btn.get("text_color") or "#ffffff"
        import re

        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#28a745" if btn_type == "link" else "#0284c7"
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", text_color):
            text_color = "#ffffff"

        if btn_type == "link":
            label = str(btn.get("label", "")).strip()
            url = str(btn.get("url", "")).strip()
            if not label:
                return None, f"Link button #{i+1} missing label."
            if not url:
                return None, f"Link button #{i+1} missing URL."
            validated.append(
                {
                    "type": "link",
                    "color": color,
                    "text_color": text_color,
                    "label": label,
                    "url": url,
                    "icon": icon,
                }
            )
        elif btn_type == "vcard":
            button_label = str(btn.get("button_label", "")).strip() or "Save Contact"
            slug = sanitize_slug(str(btn.get("slug", "")))
            if slug in seen_slugs:
                return None, f"Duplicate vCard slug '{slug}'"
            seen_slugs.add(slug)
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
                errors["phone"] = "Invalid phone format."
            if not validate_vcard_website_url(website_url):
                errors["url"] = "Enter a valid HTTP(S) URL."
            if errors:
                field_errors[i] = errors
            validated.append(
                {
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
                }
            )
        else:
            return None, f"Button #{i+1} unrecognized type: {btn_type!r}"

    if field_errors:
        return None, field_errors
    return validated, None


# -----------------------------------------------------------------------------
# Static Site Baking
# -----------------------------------------------------------------------------
def bake_static_site():
    import shutil

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for entry in os.scandir(OUTPUT_DIR):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.remove(entry.path)

    favicon_filename = logo_filename = None
    with get_db() as db:
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

    data = get_site_data()
    # Export selected icons
    for icon_name in sorted(
        {btn.get("icon") for btn in data.get("buttons", []) if btn.get("icon")}
    ):
        try:
            svg_bytes = get_icon_svg(icon_name)
            with open(os.path.join(OUTPUT_DIR, f"icon-{icon_name}.svg"), "wb") as f:
                f.write(svg_bytes)
        except KeyError:
            raise ValueError(f"Unknown brand icon: {icon_name!r}")

    processed_buttons = []
    for btn in data.get("buttons", []):
        if btn["type"] == "link":
            processed_buttons.append(
                {
                    "type": "link",
                    "color": btn.get("color", "#28a745"),
                    "text_color": btn.get("text_color", "#ffffff"),
                    "label": btn["label"],
                    "icon_filename": (
                        f"./icon-{btn['icon']}.svg" if btn.get("icon") else None
                    ),
                    "target_url": normalize_url(btn["url"]),
                }
            )
        elif btn["type"] == "vcard":
            slug = sanitize_slug(btn.get("slug", "contact"))
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
                        f"./icon-{btn['icon']}.svg" if btn.get("icon") else None
                    ),
                    "target_url": f"./{slug}.vcf",
                }
            )

    ctx = {
        "title": data["title"],
        "bio": data["bio"],
        "theme": data.get("theme", "auto"),
        "background_light": data.get("background_light", DEFAULT_LIGHT_BACKGROUND),
        "background_dark": data.get("background_dark", DEFAULT_DARK_BACKGROUND),
        "favicon_filename": favicon_filename,
        "org_logo_filename": logo_filename,
        "buttons": processed_buttons,
    }
    html = render_template("site_template.jinja2", data=ctx)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def get_icon_svg(icon_name: str) -> bytes:
    if not ICON_NAME_RE.fullmatch(icon_name):
        raise KeyError(icon_name)
    catalog = load_icon_catalog()
    if icon_name not in catalog["icons"]:
        raise KeyError(icon_name)
    path = os.path.join(
        os.path.dirname(catalog.get("_path", "static/icons/logos.json")),
        "svg",
        f"{icon_name}.svg",
    )
    # Fallback to common path structure if _path isn't set
    from common import ICON_SVG_DIR

    path = os.path.join(ICON_SVG_DIR, f"{icon_name}.svg")
    with open(path, "rb") as f:
        return sanitize_svg(f.read())


def get_icon_catalog_metadata():
    catalog = load_icon_catalog()
    return [
        {"id": n, "name": n.replace("-", " ").title()}
        for n in sorted(
            catalog["icons"], key=lambda v: v.replace("-", " ").title().casefold()
        )
    ]


def store_uploaded_image(db, file_field_name, blob_key, ext_key):
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
        "INSERT OR REPLACE INTO site_config (key, value) VALUES (?, ?)", (ext_key, ext)
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(
            url_for("change_password")
            if current_user.password_change_required
            else url_for("admin")
        )
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db() as db:
            row = db.execute(
                "SELECT id, username, password_hash, password_change_required FROM users WHERE username=?",
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
            return redirect(
                url_for("change_password")
                if user.password_change_required
                else url_for("admin")
            )
        flash("Invalid username or password.", "danger")
    return render_template("login.jinja2")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def admin():
    if current_user.password_change_required:
        return redirect(url_for("change_password"))
    data = get_site_data()
    try:
        icon_catalog = get_icon_catalog_metadata()
    except Exception:
        icon_catalog = []
        flash("Icons unavailable.", "danger")
    return render_template(
        "admin.jinja2", data=data, user=current_user, icon_catalog=icon_catalog
    )


@app.route("/icons/logos/<icon_name>.svg")
@login_required
def logo_icon(icon_name):
    try:
        return Response(get_icon_svg(icon_name), mimetype="image/svg+xml")
    except (KeyError, ValueError):
        return ("Unknown brand icon.", 404)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        cur = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(current_user.password_hash, cur):
            flash("Current password incorrect.", "danger")
        elif not new:
            flash("New password cannot be empty.", "danger")
        elif new != confirm:
            flash("Passwords do not match.", "danger")
        elif new == cur:
            flash("New password must differ.", "danger")
        else:
            h = generate_password_hash(new, method="scrypt")
            with get_db() as db:
                db.execute(
                    "UPDATE users SET password_hash=?, password_change_required=0 WHERE id=?",
                    (h, current_user.id),
                )
            current_user.password_hash = h
            current_user.password_change_required = False
            flash("Password changed.", "success")
            return redirect(url_for("admin"))
    return render_template("change_password.jinja2")


@app.route("/save", methods=["POST"])
@login_required
def save():
    if current_user.password_change_required:
        return redirect(url_for("change_password"))
    title = request.form.get("site_title", "")
    bio = request.form.get("bio", "")
    theme = request.form.get("theme", "auto")
    bg_l = request.form.get("background_light", DEFAULT_LIGHT_BACKGROUND).strip()
    bg_d = request.form.get("background_dark", DEFAULT_DARK_BACKGROUND).strip()
    raw_btns = request.form.get("buttons_json", "[]")

    parsed_buttons, error = validate_buttons_payload(raw_btns)
    try:
        posted_btns = json.loads(raw_btns)
    except:
        posted_btns = []
    try:
        icon_cat = get_icon_catalog_metadata()
    except:
        icon_cat = []

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
        flash("Invalid theme.", "danger")
        return (
            render_template(
                "admin.jinja2", data=state, user=current_user, icon_catalog=icon_cat
            ),
            400,
        )
    if not validate_hex_color(bg_l) or not validate_hex_color(bg_d):
        flash("Invalid hex colors.", "danger")
        return (
            render_template(
                "admin.jinja2", data=state, user=current_user, icon_catalog=icon_cat
            ),
            400,
        )
    if error:
        flash(f"Error: {error}", "danger")
        return (
            render_template(
                "admin.jinja2", data=state, user=current_user, icon_catalog=icon_cat
            ),
            400,
        )

    try:
        import sqlite3

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
        flash(f"Image error: {e}", "danger")
        return (
            render_template(
                "admin.jinja2", data=state, user=current_user, icon_catalog=icon_cat
            ),
            400,
        )
    except Exception as e:
        app.logger.error(f"DB Error: {e}")
        flash("Database error.", "danger")
        return (
            render_template(
                "admin.jinja2", data=state, user=current_user, icon_catalog=icon_cat
            ),
            500,
        )

    try:
        bake_static_site()
        flash("Saved & baked!", "success")
    except Exception as e:
        app.logger.error(f"Bake error: {e}")
        flash("Saved but bake failed.", "danger")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

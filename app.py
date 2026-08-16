import os
import re
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
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

app = Flask(__name__)

# SECURITY: Secret key configuration
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

# Trust reverse proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

DB_PATH = os.environ.get("DB_PATH", "app.db")
OUTPUT_DIR = os.path.abspath(os.environ.get("OUTPUT_DIR", "/srv/www/example.com/@info"))

DEPLOYMENT_SUPERADMIN_USER = os.environ.get("SUPERADMIN_USER") or "admin"
DEPLOYMENT_SUPERADMIN_PASS = (
    os.environ.get("SUPERADMIN_PASSWORD") or "SuperSecretPass123!"
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash


@login_manager.user_loader
def load_user(user_id):
    with get_db() as db:
        user_row = db.execute(
            "SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user_row:
            return User(
                id=user_row["id"],
                username=user_row["username"],
                password_hash=user_row["password_hash"],
            )
    return None


@contextmanager
def get_db():
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


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS site_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                blob_value BLOB
            )
            """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                color TEXT DEFAULT '#0066cc'
            )
            """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS button_links (
                button_id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                FOREIGN KEY (button_id) REFERENCES buttons(id) ON DELETE CASCADE
            )
            """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS button_vcards (
                button_id INTEGER PRIMARY KEY,
                button_label TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                fn TEXT,
                org TEXT,
                title TEXT,
                email TEXT,
                phone TEXT,
                url TEXT,
                FOREIGN KEY (button_id) REFERENCES buttons(id) ON DELETE CASCADE
            )
            """)

        db.execute(
            """
            INSERT INTO users (username, password_hash)
            VALUES (?, ?)
            ON CONFLICT(username) DO NOTHING
            """,
            (
                DEPLOYMENT_SUPERADMIN_USER,
                generate_password_hash(DEPLOYMENT_SUPERADMIN_PASS, method="scrypt"),
            ),
        )


@app.cli.command("init-db")
def init_db_command():
    init_db()
    try:
        bake_static_site()
        print("Database schema initialized and initial static bake complete.")
    except Exception as e:
        app.logger.warning(f"Initial bake failed: {e}")


def get_site_data():
    with get_db() as db:
        config_rows = db.execute(
            "SELECT key, value, blob_value FROM site_config"
        ).fetchall()
        config = {}
        has_favicon = False
        has_org_logo = False

        for row in config_rows:
            config[row["key"]] = row["value"]
            if row["key"] == "favicon_blob" and row["blob_value"]:
                has_favicon = True
            if row["key"] == "org_logo_blob" and row["blob_value"]:
                has_org_logo = True

        buttons_rows = db.execute(
            "SELECT id, type, position, color FROM buttons ORDER BY position ASC, id ASC"
        ).fetchall()
        buttons = []

        for b in buttons_rows:
            b_id, b_type, b_color = b["id"], b["type"], b["color"] or "#0066cc"
            if b_type == "link":
                l_row = db.execute(
                    "SELECT label, url FROM button_links WHERE button_id = ?", (b_id,)
                ).fetchone()
                if l_row:
                    buttons.append(
                        {
                            "id": b_id,
                            "type": "link",
                            "color": b_color,
                            "label": l_row["label"],
                            "url": l_row["url"],
                        }
                    )
            elif b_type == "vcard":
                v_row = db.execute(
                    "SELECT button_label, slug, fn, org, title, email, phone, url FROM button_vcards WHERE button_id = ?",
                    (b_id,),
                ).fetchone()
                if v_row:
                    buttons.append(
                        {
                            "id": b_id,
                            "type": "vcard",
                            "color": b_color,
                            "button_label": v_row["button_label"],
                            "slug": v_row["slug"],
                            "fn": v_row["fn"],
                            "org": v_row["org"],
                            "title": v_row["title"],
                            "email": v_row["email"],
                            "phone": v_row["phone"],
                            "url": v_row["url"],
                        }
                    )

        fav_ext = config.get("favicon_ext", ".ico")
        logo_ext = config.get("org_logo_ext", ".png")

        return {
            "title": config.get("title", ""),
            "bio": config.get("bio", ""),
            "theme": config.get("theme", "auto"),
            "has_favicon": has_favicon,
            "has_org_logo": has_org_logo,
            "favicon_filename": f"favicon{fav_ext}",
            "org_logo_filename": f"logo{logo_ext}",
            "buttons": buttons,
        }


def sanitize_slug(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_\-]", "_", name)
    return name or "contact"


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/") or url.startswith("."):
        return url
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url}"
    return url


def clean_vcard_field(val: str) -> str:
    if not val:
        return ""
    val = val.replace("\r", "").replace("\n", " ")
    return val.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


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


def purge_stale_assets(directory: str, prefix: str):
    """Purge stale extension variants from destination directory prior to writing."""
    if not os.path.exists(directory):
        return
    for filename in os.listdir(directory):
        if filename.startswith(f"{prefix}."):
            try:
                os.remove(os.path.join(directory, filename))
            except OSError:
                pass


def bake_static_site():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    favicon_filename = None
    logo_filename = None

    with get_db() as db:
        # Export favicon
        favicon_row = db.execute(
            "SELECT blob_value FROM site_config WHERE key = 'favicon_blob'"
        ).fetchone()

        if favicon_row and favicon_row["blob_value"]:
            ext_row = db.execute(
                "SELECT value FROM site_config WHERE key = 'favicon_ext'"
            ).fetchone()
            ext = ext_row["value"] if ext_row and ext_row["value"] else ".ico"
            favicon_filename = f"favicon{ext}"

            purge_stale_assets(OUTPUT_DIR, "favicon")
            with open(os.path.join(OUTPUT_DIR, favicon_filename), "wb") as f:
                f.write(favicon_row["blob_value"])

        # Export organization logo
        logo_row = db.execute(
            "SELECT blob_value FROM site_config WHERE key = 'org_logo_blob'"
        ).fetchone()

        if logo_row and logo_row["blob_value"]:
            ext_row = db.execute(
                "SELECT value FROM site_config WHERE key = 'org_logo_ext'"
            ).fetchone()
            ext = ext_row["value"] if ext_row and ext_row["value"] else ".png"
            logo_filename = f"logo{ext}"

            purge_stale_assets(OUTPUT_DIR, "logo")
            with open(os.path.join(OUTPUT_DIR, logo_filename), "wb") as f:
                f.write(logo_row["blob_value"])

    data = get_site_data()
    vcard_dir = os.path.join(OUTPUT_DIR, "vcard")
    os.makedirs(vcard_dir, exist_ok=True)

    processed_buttons = []
    for btn in data.get("buttons", []):
        if btn["type"] == "link":
            processed_buttons.append(
                {
                    "type": "link",
                    "color": btn.get("color", "#0066cc"),
                    "label": btn["label"],
                    "target_url": normalize_url(btn["url"]),
                }
            )
        elif btn["type"] == "vcard":
            slug = sanitize_slug(btn.get("slug", "contact"))
            vcard_filename = f"{slug}.vcf"
            vcard_path = os.path.join(vcard_dir, vcard_filename)

            vcard_data = {
                "fn": btn.get("fn", ""),
                "org": btn.get("org", ""),
                "title": btn.get("title", ""),
                "email": btn.get("email", ""),
                "phone": btn.get("phone", ""),
                "url": normalize_url(btn.get("url", "")),
            }

            with open(vcard_path, "w", encoding="utf-8") as f:
                f.write(generate_vcard_content(vcard_data))

            processed_buttons.append(
                {
                    "type": "vcard",
                    "color": btn.get("color", "#0066cc"),
                    "label": btn.get("button_label", "Save Contact"),
                    "target_url": f"./vcard/{vcard_filename}",
                }
            )

    render_context = {
        "title": data["title"],
        "bio": data["bio"],
        "theme": data.get("theme", "auto"),
        "favicon_filename": favicon_filename,
        "org_logo_filename": logo_filename,
        "buttons": processed_buttons,
    }

    rendered_html = render_template("site_template.jinja2", data=render_context)
    index_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(rendered_html)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_db() as db:
            user_row = db.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if user_row and check_password_hash(user_row["password_hash"], password):
            user = User(
                id=user_row["id"],
                username=user_row["username"],
                password_hash=user_row["password_hash"],
            )
            login_user(user)
            return redirect(url_for("admin"))

        flash("Invalid username or password.", "danger")

    return render_template("login.jinja2")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def admin():
    data = get_site_data()
    return render_template("admin.jinja2", data=data, user=current_user)


@app.route("/save", methods=["POST"])
@login_required
def save():
    title = request.form.get("title", "")
    bio = request.form.get("bio", "")
    theme = request.form.get("theme", "auto")

    types = request.form.getlist("btn_type")
    colors = request.form.getlist("btn_color")

    link_labels = request.form.getlist("link_label")
    link_urls = request.form.getlist("link_url")

    vcard_button_labels = request.form.getlist("vcard_button_label")
    vcard_slugs = request.form.getlist("vcard_slug")
    vcard_fns = request.form.getlist("vcard_fn")
    vcard_orgs = request.form.getlist("vcard_org")
    vcard_titles = request.form.getlist("vcard_title")
    vcard_emails = request.form.getlist("vcard_email")
    vcard_phones = request.form.getlist("vcard_phone")
    vcard_urls = request.form.getlist("vcard_url")

    parsed_buttons = []
    sanitized_slugs = []

    l_idx = 0
    v_idx = 0

    for idx, b_type in enumerate(types):
        b_color = colors[idx] if idx < len(colors) else "#0066cc"
        if b_type == "link":
            parsed_buttons.append(
                {
                    "type": "link",
                    "color": b_color,
                    "label": link_labels[l_idx] if l_idx < len(link_labels) else "",
                    "url": link_urls[l_idx] if l_idx < len(link_urls) else "",
                }
            )
            l_idx += 1
        elif b_type == "vcard":
            raw_slug = vcard_slugs[v_idx] if v_idx < len(vcard_slugs) else "contact"
            clean_slug = sanitize_slug(raw_slug)
            sanitized_slugs.append(clean_slug)

            parsed_buttons.append(
                {
                    "type": "vcard",
                    "color": b_color,
                    "button_label": (
                        vcard_button_labels[v_idx]
                        if v_idx < len(vcard_button_labels)
                        else "Save Contact"
                    ),
                    "slug": clean_slug,
                    "fn": vcard_fns[v_idx] if v_idx < len(vcard_fns) else "",
                    "org": vcard_orgs[v_idx] if v_idx < len(vcard_orgs) else "",
                    "title": vcard_titles[v_idx] if v_idx < len(vcard_titles) else "",
                    "email": vcard_emails[v_idx] if v_idx < len(vcard_emails) else "",
                    "phone": vcard_phones[v_idx] if v_idx < len(vcard_phones) else "",
                    "url": vcard_urls[v_idx] if v_idx < len(vcard_urls) else "",
                }
            )
            v_idx += 1

    posted_state = {
        "title": title,
        "bio": bio,
        "theme": theme,
        "buttons": parsed_buttons,
    }

    if len(sanitized_slugs) != len(set(sanitized_slugs)):
        flash("Error: Duplicate vCard slugs detected.", "danger")
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            400,
        )

    try:
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

            # Process Favicon
            if "favicon" in request.files:
                file = request.files["favicon"]
                if file and file.filename != "":
                    safe_fname = secure_filename(file.filename)
                    if safe_fname:
                        _, ext = os.path.splitext(safe_fname)
                        ext = ext.lower() or ".ico"
                        blob_bytes = file.read()
                        db.execute(
                            "INSERT OR REPLACE INTO site_config (key, value, blob_value) VALUES ('favicon_blob', 'present', ?)",
                            (sqlite3.Binary(blob_bytes),),
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO site_config (key, value) VALUES ('favicon_ext', ?)",
                            (ext,),
                        )

            # Process Org Logo
            if "org_logo" in request.files:
                file = request.files["org_logo"]
                if file and file.filename != "":
                    safe_fname = secure_filename(file.filename)
                    if safe_fname:
                        _, ext = os.path.splitext(safe_fname)
                        ext = ext.lower() or ".png"
                        blob_bytes = file.read()
                        db.execute(
                            "INSERT OR REPLACE INTO site_config (key, value, blob_value) VALUES ('org_logo_blob', 'present', ?)",
                            (sqlite3.Binary(blob_bytes),),
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO site_config (key, value) VALUES ('org_logo_ext', ?)",
                            (ext,),
                        )

            # Atomic button state replacement
            db.execute("DELETE FROM button_links")
            db.execute("DELETE FROM button_vcards")
            db.execute("DELETE FROM buttons")

            for position, btn in enumerate(parsed_buttons, start=1):
                db.execute(
                    "INSERT INTO buttons (type, position, color) VALUES (?, ?, ?)",
                    (btn["type"], position, btn["color"]),
                )
                btn_id = db.lastrowid

                if btn["type"] == "link":
                    db.execute(
                        "INSERT INTO button_links (button_id, label, url) VALUES (?, ?, ?)",
                        (btn_id, btn["label"], btn["url"]),
                    )
                elif btn["type"] == "vcard":
                    db.execute(
                        """
                        INSERT INTO button_vcards (button_id, button_label, slug, fn, org, title, email, phone, url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            btn_id,
                            btn["button_label"],
                            btn["slug"],
                            btn["fn"],
                            btn["org"],
                            btn["title"],
                            btn["email"],
                            btn["phone"],
                            btn["url"],
                        ),
                    )

    except Exception as e:
        flash(f"Database error during save: {str(e)}", "danger")
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            500,
        )

    try:
        bake_static_site()
        flash("Settings saved and static site baked successfully!", "success")
    except Exception as e:
        flash(f"Database saved, but error baking static site: {str(e)}", "danger")

    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

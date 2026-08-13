import os
import sqlite3
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

app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY", "change-this-in-production-to-a-random-secret"
)

# Trust reverse proxy headers (e.g., Caddy setting X-Forwarded-Proto)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Cookie Security Configuration
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")
app.config.update(
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# --- Configuration (Overridable via Environment Variables) ---
DB_PATH = os.environ.get("DB_PATH", "app.db")
OUTPUT_DIR = os.path.abspath(os.environ.get("OUTPUT_DIR", "/srv/www/example.com/@info"))

# Initial superadmin deployment credentials
DEPLOYMENT_SUPERADMIN_USER = os.environ.get("SUPERADMIN_USER", "admin")
# Change this password before initial run or override via env var
DEPLOYMENT_SUPERADMIN_PASS = os.environ.get(
    "SUPERADMIN_PASSWORD", "SuperSecretPass123!"
)

# --- Flask-Login Setup ---
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
    conn = get_db_connection()
    user_row = conn.execute(
        "SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if user_row:
        return User(
            id=user_row["id"],
            username=user_row["username"],
            password_hash=user_row["password_hash"],
        )
    return None


# --- Database Initialization & Helpers ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)

    # Site configuration key-value table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Dynamic links table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            position INTEGER DEFAULT 0
        )
    """)

    # Seed default superadmin if no users exist
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        hashed_pw = generate_password_hash(DEPLOYMENT_SUPERADMIN_PASS, method="scrypt")
        cursor.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (DEPLOYMENT_SUPERADMIN_USER, hashed_pw),
        )
        print(f"[INIT] Superadmin created. Username: '{DEPLOYMENT_SUPERADMIN_USER}'")

    # Seed default site config if empty
    cursor.execute("SELECT COUNT(*) FROM site_config")
    if cursor.fetchone()[0] == 0:
        default_config = {
            "title": "Example Company",
            "bio": "Software Solutions",
            "vcard_fn": "John Doe",
            "vcard_org": "Example Inc.",
            "vcard_title": "Systems Engineer",
            "vcard_email": "john@example.com",
            "vcard_phone": "+1234567890",
            "vcard_url": "https://example.com",
        }
        for k, v in default_config.items():
            cursor.execute("INSERT INTO site_config (key, value) VALUES (?, ?)", (k, v))

        cursor.execute(
            "INSERT INTO links (label, url, position) VALUES (?, ?, ?)",
            ("Official Website", "https://example.com", 1),
        )
        cursor.execute(
            "INSERT INTO links (label, url, position) VALUES (?, ?, ?)",
            ("Documentation", "https://docs.example.com", 2),
        )

    conn.commit()
    conn.close()


def get_site_data():
    conn = get_db_connection()
    config_rows = conn.execute("SELECT key, value FROM site_config").fetchall()
    config = {row["key"]: row["value"] for row in config_rows}

    links_rows = conn.execute(
        "SELECT label, url FROM links ORDER BY position ASC, id ASC"
    ).fetchall()
    links = [{"label": row["label"], "url": row["url"]} for row in links_rows]
    conn.close()

    return {
        "title": config.get("title", ""),
        "bio": config.get("bio", ""),
        "vcard": {
            "fn": config.get("vcard_fn", ""),
            "org": config.get("vcard_org", ""),
            "title": config.get("vcard_title", ""),
            "email": config.get("vcard_email", ""),
            "phone": config.get("vcard_phone", ""),
            "url": config.get("vcard_url", ""),
        },
        "links": links,
    }


def sanitize_slug(name: str) -> str:
    """Sanitizes user input into a deterministic filename string."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_\-]", "_", name)
    return name or "contact"


def normalize_url(url: str) -> str:
    """Ensures external domains have https:// while preserving relative paths/schemes."""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("/") or url.startswith("."):
        return url
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url}"
    return url


# --- Static Compiler (Write-Only output) ---
def generate_vcard_content(vcard):
    return (
        "BEGIN:VCARD\n"
        "VERSION:3.0\n"
        f"FN:{vcard.get('fn', '')}\n"
        f"ORG:{vcard.get('org', '')}\n"
        f"TITLE:{vcard.get('title', '')}\n"
        f"EMAIL:{vcard.get('email', '')}\n"
        f"TEL:{vcard.get('phone', '')}\n"
        f"URL:{vcard.get('url', '')}\n"
        "END:VCARD\n"
    )


# --- Static Compiler ---
def bake_static_site():
    data = get_site_data()
    vcard_dir = os.path.join(OUTPUT_DIR, "vcard")
    os.makedirs(vcard_dir, exist_ok=True)

    vcard_info = data.get("vcard", {})
    slug = sanitize_slug(vcard_info.get("slug", "contact"))

    # 1. Write vCard using the user-specified deterministic slug
    vcard_filename = f"{slug}.vcf"
    vcard_path = os.path.join(vcard_dir, vcard_filename)

    # Normalize URL field in vCard
    vcard_info["url"] = normalize_url(vcard_info.get("url", ""))

    vcard_content = generate_vcard_content(vcard_info)
    with open(vcard_path, "w", encoding="utf-8") as f:
        f.write(vcard_content)

    # 2. Pass relative vcard path to site context so template links properly
    data["vcard_relative_path"] = f"./vcard/{vcard_filename}"

    # Normalize external link URLs
    for link in data.get("links", []):
        link["url"] = normalize_url(link["url"])

    # 3. Render index.html
    rendered_html = render_template("site_template.html", data=data)
    index_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(rendered_html)


# --- Routes ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db_connection()
        user_row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        conn.close()

        if user_row and check_password_hash(user_row["password_hash"], password):
            user = User(
                id=user_row["id"],
                username=user_row["username"],
                password_hash=user_row["password_hash"],
            )
            login_user(user)
            return redirect(url_for("admin"))

        flash("Invalid username or password.", "danger")

    return render_template("login.html")


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
    return render_template("admin.html", data=data, user=current_user)


@app.route("/save", methods=["POST"])
@login_required
def save():
    title = request.form.get("title", "")
    bio = request.form.get("bio", "")

    vcard_slug = request.form.get("vcard_slug", "")
    vcard_fn = request.form.get("vcard_fn", "")
    vcard_org = request.form.get("vcard_org", "")
    vcard_title = request.form.get("vcard_title", "")
    vcard_email = request.form.get("vcard_email", "")
    vcard_phone = request.form.get("vcard_phone", "")
    vcard_url = request.form.get("vcard_url", "")

    labels = request.form.getlist("link_label")
    urls = request.form.getlist("link_url")

    conn = get_db_connection()
    cursor = conn.cursor()

    # Update site_config table
    config_updates = {
        "title": title,
        "bio": bio,
        "vcard_slug": vcard_slug,
        "vcard_fn": vcard_fn,
        "vcard_org": vcard_org,
        "vcard_title": vcard_title,
        "vcard_email": vcard_email,
        "vcard_phone": vcard_phone,
        "vcard_url": vcard_url,
    }

    for key, val in config_updates.items():
        cursor.execute(
            "INSERT OR REPLACE INTO site_config (key, value) VALUES (?, ?)", (key, val)
        )

    # Replace links in database
    cursor.execute("DELETE FROM links")
    position = 1
    for label, url in zip(labels, urls):
        if label.strip() and url.strip():
            cursor.execute(
                "INSERT INTO links (label, url, position) VALUES (?, ?, ?)",
                (label.strip(), url.strip(), position),
            )
            position += 1

    conn.commit()
    conn.close()

    # Bake out flat HTML and .vcf to static directory
    try:
        bake_static_site()
        flash("Settings saved and static site baked successfully!", "success")
    except Exception as e:
        flash(f"Database saved, but error baking static site: {str(e)}", "danger")

    return redirect(url_for("admin"))


if __name__ == "__main__":
    init_db()
    # Initial compilation on startup to ensure public directory matches DB state
    try:
        bake_static_site()
    except Exception as e:
        print(f"[WARN] Initial bake failed: {e}")

    # Bind strictly to 127.0.0.1 / internal interface
    app.run(host="127.0.0.1", port=5000, debug=False)

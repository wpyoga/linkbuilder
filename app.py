# Import standard library modules for OS path operations and file manipulation
import os
import re
import io
import shutil
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

# Import PyFilesystem2 modules for abstracted filesystem directory operations
from fs import open_fs
from fs.errors import FSError

# Initialize the main Flask application instance
app = Flask(__name__)

# Configure application secret key using environment variables or fall back to standard random bytes
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

# Apply proxy fix middleware to handle HTTP headers correctly behind reverse proxies like Nginx
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Evaluate security boolean from environment variables for cookie handling
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")

# Apply Flask configuration parameters for session security and file size limits
app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

# Resolve default paths for the SQLite database and the static export destination directory
DB_PATH = os.environ.get("DB_PATH", "app.db")
OUTPUT_DIR = os.path.abspath(os.environ.get("OUTPUT_DIR", "/srv/www/example.com/@info"))

# Define administrative credential defaults for initial database bootstrapping
DEPLOYMENT_SUPERADMIN_USER = os.environ.get("SUPERADMIN_USER") or "admin"
DEPLOYMENT_SUPERADMIN_PASS = (
    os.environ.get("SUPERADMIN_PASSWORD") or "SuperSecretPass123!"
)

# Instantiate and bind the login manager to handle user session lifecycle
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# Define the user model class extending UserMixin for compatibility with Flask-Login
class User(UserMixin):
    # Initialize the user instance with primary ID, username, and password hash
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash


# Register user loader callback function for retrieving session users by database primary key
@login_manager.user_loader
def load_user(user_id):
    # Retrieve user credentials from the database within context safety
    with get_db() as db:
        user_row = db.execute(
            "SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        # Instantiate user object if corresponding row exists in the database
        if user_row:
            return User(
                id=user_row["id"],
                username=user_row["username"],
                password_hash=user_row["password_hash"],
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


# Database initialization routine to set up tables, indexes, and default admin account
def init_db():
    # Ensure directory path for SQLite file exists prior to connection creation
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db() as db:
        # Create users table for administrative access control
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            """)

        # Create site configuration table for system values and image blobs
        db.execute("""
            CREATE TABLE IF NOT EXISTS site_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                blob_value BLOB
            )
            """)

        # Create main tracking table for link and vcard action buttons
        db.execute("""
            CREATE TABLE IF NOT EXISTS buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                color TEXT DEFAULT '#0066cc'
            )
            """)

        # Create child table storing URL link button metadata
        db.execute("""
            CREATE TABLE IF NOT EXISTS button_links (
                button_id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                FOREIGN KEY (button_id) REFERENCES buttons(id) ON DELETE CASCADE
            )
            """)

        # Create child table storing vCard contact download metadata
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

        # Optimization 1: Create explicit indexes for button positioning and slug lookups
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_buttons_position ON buttons(position, id);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_button_vcards_slug ON button_vcards(slug);"
        )

        # Seed initial administrative user into database if absent
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


# Register CLI command for initializing database and triggering clean initial static bake
@app.cli.command("init-db")
def init_db_command():
    init_db()
    try:
        bake_static_site()
        print("Database schema initialized and initial static bake complete.")
    except Exception as e:
        app.logger.warning(f"Initial bake failed: {e}")


# Helper function to query complete site state and button lists from SQLite storage
# Optimization 2: Combined multiple isolated loop queries into continuous join operations to prevent N+1 overhead
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

        # Fetch button entries with LEFT JOIN across child tables to retrieve metadata in a single query pass
        query = """
            SELECT 
                b.id, b.type, b.color,
                l.label AS link_label, l.url AS link_url,
                v.button_label AS vcard_button_label, v.slug AS vcard_slug,
                v.fn, v.org, v.title, v.email, v.phone, v.url AS vcard_url
            FROM buttons b
            LEFT JOIN button_links l ON b.id = l.button_id
            LEFT JOIN button_vcards v ON b.id = v.button_id
            ORDER BY b.position ASC, b.id ASC
        """
        buttons_rows = db.execute(query).fetchall()
        buttons = []

        # Parse unified join output directly into the structured list
        for b in buttons_rows:
            b_id, b_type, b_color = b["id"], b["type"], b["color"] or "#0066cc"
            if b_type == "link" and b["link_label"] is not None:
                buttons.append(
                    {
                        "id": b_id,
                        "type": "link",
                        "color": b_color,
                        "label": b["link_label"],
                        "url": b["link_url"],
                    }
                )
            elif b_type == "vcard" and b["vcard_slug"] is not None:
                buttons.append(
                    {
                        "id": b_id,
                        "type": "vcard",
                        "color": b_color,
                        "button_label": b["vcard_button_label"],
                        "slug": b["vcard_slug"],
                        "fn": b["fn"],
                        "org": b["org"],
                        "title": b["title"],
                        "email": b["email"],
                        "phone": b["phone"],
                        "url": b["vcard_url"],
                    }
                )

        # Resolve asset file extensions or apply sensible defaults
        fav_ext = config.get("favicon_ext", ".ico")
        logo_ext = config.get("org_logo_ext", ".png")

        # Return standardized structured data dictionary for view models and renders
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


# Sanitize slug input to guarantee URL-safe file paths for vCards
def sanitize_slug(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_\-]", "_", name)
    return name or "contact"


# Normalize web URLs to enforce absolute scheme prefixes when missing
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


# Clean a target output directory safely using PyFilesystem2 abstractions.
# PROCESS EXPLANATION:
# 1. os.makedirs(directory, exist_ok=True) ensures the target directory exists on the native OS filesystem so open_fs() can acquire a valid handle.
# 2. open_fs(directory) initializes an OSFS (OS Filesystem) context bounded to the specified path, providing explicit filesystem safety boundaries.
# 3. output_fs.removetree("/") recursively purges all contained files, symlinks, and child subdirectories while preserving the top-level directory instance itself.
# 4. FSError exceptions are trapped and logged to prevent filesystem-level unlinking lock errors from bubbling up to the WSGI application loop.
def clean_output_directory(directory: str):
    os.makedirs(directory, exist_ok=True)
    try:
        with open_fs(directory) as output_fs:
            output_fs.removetree("/")
    except FSError as e:
        app.logger.error(f"Failed to clear output directory via PyFilesystem2: {e}")
        raise


# Core site generation routine that purges target path and writes generated static assets
# Optimization 4: Streamlined buffer writes via io.BytesIO and io.StringIO with shutil.copyfileobj to optimize disk I/O
def bake_static_site():
    # Purge all existing artifacts and recreate the root target directory using the utility wrapper
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

            # Write binary stream directly using buffer memory transfer
            with open(os.path.join(OUTPUT_DIR, favicon_filename), "wb") as f_out:
                buf = io.BytesIO(favicon_row["blob_value"])
                shutil.copyfileobj(buf, f_out)

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

            # Write raw binary image payload to target using buffered streaming
            with open(os.path.join(OUTPUT_DIR, logo_filename), "wb") as f_out:
                buf = io.BytesIO(logo_row["blob_value"])
                shutil.copyfileobj(buf, f_out)

    # Retrieve current structured state from SQLite backend
    data = get_site_data()
    vcard_dir = os.path.join(OUTPUT_DIR, "vcard")
    os.makedirs(vcard_dir, exist_ok=True)

    processed_buttons = []
    # Process configuration buttons and compile individual vCard files
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

            # Generate individual vCard payload stream and write using buffer pipe
            vcard_content = generate_vcard_content(vcard_data)
            with open(vcard_path, "w", encoding="utf-8") as f_out:
                buf = io.StringIO(vcard_content)
                shutil.copyfileobj(buf, f_out)

            processed_buttons.append(
                {
                    "type": "vcard",
                    "color": btn.get("color", "#0066cc"),
                    "label": btn.get("button_label", "Save Contact"),
                    "target_url": f"./vcard/{vcard_filename}",
                }
            )

    # Compile context object for Jinja2 template engine execution
    render_context = {
        "title": data["title"],
        "bio": data["bio"],
        "theme": data.get("theme", "auto"),
        "favicon_filename": favicon_filename,
        "org_logo_filename": logo_filename,
        "buttons": processed_buttons,
    }

    # Render Jinja template into output HTML string and write index file via memory buffer
    rendered_html = render_template("site_template.jinja2", data=render_context)
    index_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f_out:
        buf = io.StringIO(rendered_html)
        shutil.copyfileobj(buf, f_out)


# Handle login form displays and authentication post requests
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

        # Verify hashed password string against user submission
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
    data = get_site_data()
    return render_template("admin.jinja2", data=data, user=current_user)


# Handle configuration modifications, file uploads, and trigger dynamic bakes
# Optimization 5: Enforced complete single-transaction boundary guarantees across multi-table writes
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

    # Parse dynamic list structures from POST payload
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

    # Validate slug uniqueness across all submitted vCard items
    if len(sanitized_slugs) != len(set(sanitized_slugs)):
        flash("Error: Duplicate vCard slugs detected.", "danger")
        return (
            render_template("admin.jinja2", data=posted_state, user=current_user),
            400,
        )

    try:
        # Atomic database transaction block spanning state modifications and button table updates
        with get_db() as db:
            # Update base site variables in configuration table
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

            # Store uploaded binary favicon stream if present in upload form
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

            # Store uploaded binary organization logo stream if present in form
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

            # Clear button relational tables atomically
            db.execute("DELETE FROM button_links")
            db.execute("DELETE FROM button_vcards")
            db.execute("DELETE FROM buttons")

            # Insert updated button structures and related detail records within active transaction
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

    # Execute static bake step to generate site files on disk
    try:
        bake_static_site()
        flash("Settings saved and static site baked successfully!", "success")
    except Exception as e:
        flash(f"Database saved, but error baking static site: {str(e)}", "danger")

    return redirect(url_for("admin"))


# Run development server if executed directly as entrypoint script
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

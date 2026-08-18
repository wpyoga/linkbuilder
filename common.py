import os
import re
import sqlite3
from contextlib import contextmanager

# -----------------------------------------------------------------------------
# Configuration & Paths
# -----------------------------------------------------------------------------
SECRET_KEY_FILE = os.environ.get("SECRET_KEY_FILE", "secret_key")

DB_PATH = os.path.abspath(
    os.environ.get(
        "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
    )
)

OUTPUT_DIR = os.path.abspath(os.environ.get("OUTPUT_DIR", "/srv/www/example.com/@info"))

# Icon paths
ICON_BASE_DIR = os.path.abspath(os.environ.get("ICON_BASE_DIR", "static/icons"))
ICON_CATALOG_SRC_URL = os.environ.get(
    "ICON_CATALOG_URL",
    "https://raw.githubusercontent.com/iconify/icon-sets/master/json/logos.json",
)

# We generate our own filtered catalog
CUSTOM_ICON_CATALOG_PATH = os.path.join(ICON_BASE_DIR, "custom_logos.json")
ICON_SVG_DIR = os.path.join(ICON_BASE_DIR, "svg")

ICON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

DEPLOYMENT_SUPERADMIN_USER = os.environ.get("SUPERADMIN_USER") or "admin"

DEFAULT_LIGHT_BACKGROUND = "#f8fafc"
DEFAULT_DARK_BACKGROUND = "#0f172a"
ALLOWED_THEMES = {"auto", "light", "dark"}

RASTER_FORMAT_EXTENSIONS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "GIF": ".gif",
    "WEBP": ".webp",
    "ICO": ".ico",
    "BMP": ".bmp",
}


# -----------------------------------------------------------------------------
# Database Utilities
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Shared Validation Helpers
# -----------------------------------------------------------------------------
def validate_hex_color(value: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", (value or "").strip()))


def sanitize_slug(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9_\-]", "_", name)
    return name or "contact"

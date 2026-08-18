import os
import re

# Resolve base directory for relative path anchoring.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# -----------------------------------------------------------------------------
# Database & Secrets
# -----------------------------------------------------------------------------
# DB_PATH is made absolute immediately (not left as the raw env value / relative default)
# because sqlite3.connect() resolves a relative path against the CURRENT PROCESS's working
# directory at connect time. If `init-db` is run from one working directory and the app is
# later started from a different one, a relative DB_PATH silently resolves to two different
# files. Resolving to an absolute path once here ensures both commands always agree.
DB_PATH = os.path.abspath(os.environ.get("DB_PATH", os.path.join(BASE_DIR, "app.db")))

# Resolve the path to the secret key file. The key file is generated once by `init-db`
# and read here at every startup. It can be overridden by an env var for deployments that
# prefer to manage secrets externally.
SECRET_KEY_FILE = os.environ.get(
    "SECRET_KEY_FILE", os.path.join(BASE_DIR, "secret_key")
)

# -----------------------------------------------------------------------------
# Icon Configuration
# -----------------------------------------------------------------------------
# Icons are stored in a dedicated 'icons' directory. SVGs are pre-sanitized and filtered
# by sync-icons.py, then served statically by app.py.
ICON_DIR = os.path.abspath(os.environ.get("ICON_DIR", os.path.join(BASE_DIR, "icons")))
ICON_SVG_DIR = os.path.join(ICON_DIR, "svg")

# Source URL for the upstream Iconify catalog. We download this, filter it, and store
# our own custom catalog in the database.
ICON_SRC_URL = os.environ.get(
    "ICON_CATALOG_URL",
    "https://raw.githubusercontent.com/iconify/icon-sets/master/json/logos.json",
)

# -----------------------------------------------------------------------------
# App Constants & Validation Patterns
# -----------------------------------------------------------------------------
ALLOWED_THEMES = {"auto", "light", "dark"}
DEFAULT_LIGHT_BG = "#f8fafc"
DEFAULT_DARK_BG = "#0f172a"

# Regex for validating hex colors (e.g., #ffffff). Used in both admin validation and
# site data resolution.
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Regex for validating icon identifiers. Must be lowercase alphanumeric with hyphens.
# This matches the naming convention of the Iconify 'logos' set.
ICON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Administrative bootstrap username. Unlike the password, a predictable default username
# is not a meaningful security weakness on its own (usernames aren't secret).
DEPLOYMENT_SUPERADMIN_USER = os.environ.get("SUPERADMIN_USER") or "admin"

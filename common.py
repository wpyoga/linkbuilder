import os
import re
import io
import json
import math
import sqlite3
import urllib.request
from contextlib import contextmanager

# Third-party imports for shared logic
from PIL import Image, UnidentifiedImageError
import defusedxml.ElementTree as DefusedET
import xml.etree.ElementTree as ET
from svgelements import SVG

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

ICON_CATALOG_PATH = os.path.abspath(
    os.environ.get("ICON_CATALOG_PATH", "static/icons/logos.json")
)
ICON_SVG_DIR = os.path.join(os.path.dirname(ICON_CATALOG_PATH), "svg")
ICON_CATALOG_URL = os.environ.get(
    "ICON_CATALOG_URL",
    "https://raw.githubusercontent.com/iconify/icon-sets/master/json/logos.json",
)
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

SVG_DISALLOWED_TAGS = {"script", "foreignObject", "iframe"}
SVG_DISALLOWED_ATTR_PREFIXES = ("on",)
SVG_DISALLOWED_ATTRS = {"href", "xlink:href"}


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
# Icon / SVG Utilities
# -----------------------------------------------------------------------------
def calculate_svg_bbox(svg):
    bbox = None
    for element in svg.elements():
        try:
            if (
                element.values.get("visibility") == "hidden"
                or element.values.get("display") == "none"
            ):
                continue
        except AttributeError:
            pass
        try:
            element_bbox = element.bbox()
        except Exception as e:
            print(f"Warning: {e}")
            continue
        if element_bbox is None:
            continue
        if not all(math.isfinite(value) for value in element_bbox):
            raise ValueError(f"Element has non-finite bounds: {element_bbox}")
        xmin, ymin, xmax, ymax = element_bbox
        if bbox is None:
            bbox = [xmin, ymin, xmax, ymax]
        else:
            bbox[0] = min(bbox[0], xmin)
            bbox[1] = min(bbox[1], ymin)
            bbox[2] = max(bbox[2], xmax)
            bbox[3] = max(bbox[3], ymax)
    if bbox is None:
        raise ValueError("SVG contains no drawable geometry")
    return tuple(bbox)


def normalize_icon_to_file(icon_name, icon, catalog, output_dir):
    source_width = icon.get("width", catalog.get("width", 24))
    source_height = icon.get("height", catalog.get("height", 24))
    body = icon.get("body", "")
    xmlns = 'xmlns="http://www.w3.org/2000/svg"'
    if "xlink:href=" in body:
        xmlns += ' xmlns:xlink="http://w3.org"'

    body_root = DefusedET.fromstring(f"<svg {xmlns}>{body}</svg>")
    root = ET.Element(
        "{http://www.w3.org/2000/svg}svg",
        {
            "width": str(source_width),
            "height": str(source_height),
            "viewBox": f"0 0 {source_width} {source_height}",
        },
    )
    root.extend(body_root)

    svg_bytes = ET.tostring(root, encoding="utf-8")
    svg = SVG.parse(io.BytesIO(svg_bytes))
    xmin, ymin, xmax, ymax = calculate_svg_bbox(svg)
    width = xmax - xmin
    height = ymax - ymin

    if width <= 0 or height <= 0:
        raise ValueError(f"Icon {icon_name!r} has invalid bounding box")
    if width == 0 or not 0.75 <= height / width <= 1.33:
        return

    root.set("viewBox", f"{xmin:g} {ymin:g} {width:g} {height:g}")
    root.set("width", f"{width:g}")
    root.set("height", f"{height:g}")
    ET.register_namespace("", "http://www.w3.org/2000/svg")

    output_path = os.path.join(output_dir, f"{icon_name}.svg")
    temporary_path = f"{output_path}.tmp"
    ET.ElementTree(root).write(temporary_path, encoding="utf-8", xml_declaration=True)
    os.replace(temporary_path, output_path)


def sync_icon_catalog():
    os.makedirs(os.path.dirname(ICON_CATALOG_PATH), exist_ok=True)
    os.makedirs(ICON_SVG_DIR, exist_ok=True)

    request = urllib.request.Request(
        ICON_CATALOG_URL, headers={"User-Agent": "Linkbuilder/1.0"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read()

    catalog = json.loads(payload)
    if catalog.get("prefix") != "logos" or not isinstance(catalog.get("icons"), dict):
        raise ValueError("Downloaded SVG Logos catalog has an unexpected format.")

    for icon_name, icon in catalog["icons"].items():
        print(f"Processing icon: {icon_name}")
        normalize_icon_to_file(icon_name, icon, catalog, ICON_SVG_DIR)

    temporary_path = f"{ICON_CATALOG_PATH}.tmp"
    with open(temporary_path, "wb") as f_out:
        f_out.write(payload)
    os.replace(temporary_path, ICON_CATALOG_PATH)
    return catalog


def load_icon_catalog():
    try:
        with open(ICON_CATALOG_PATH, "r", encoding="utf-8") as f_in:
            catalog = json.load(f_in)
    except FileNotFoundError:
        catalog = sync_icon_catalog()
    if catalog.get("prefix") != "logos" or not isinstance(catalog.get("icons"), dict):
        raise ValueError("Local SVG Logos catalog has an unexpected format.")
    return catalog


def sanitize_svg(raw_bytes):
    try:
        root = DefusedET.fromstring(raw_bytes)
    except Exception as e:
        raise ValueError(f"File is not valid SVG/XML: {e}")

    def strip_attrs(el):
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
            local_tag = child.tag.split("}")[-1] if isinstance(child.tag, str) else ""
            if local_tag in SVG_DISALLOWED_TAGS:
                el.remove(child)
                continue
            strip(child)

    strip(root)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="utf-8")


def validate_and_normalize_image(file_storage):
    raw_bytes = file_storage.read()
    if not raw_bytes:
        raise ValueError("Uploaded file is empty.")
    head = raw_bytes[:512].lstrip().lower()
    looks_like_svg = head.startswith(b"<?xml") or b"<svg" in head
    if looks_like_svg:
        return sanitize_svg(raw_bytes), ".svg"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()
        with Image.open(io.BytesIO(raw_bytes)) as img:
            fmt = img.format
    except UnidentifiedImageError:
        raise ValueError("File is not a recognized image format.")
    except Exception as e:
        raise ValueError(f"Uploaded image failed validation: {e}")
    ext = RASTER_FORMAT_EXTENSIONS.get(fmt)
    if not ext:
        raise ValueError(f"Image format '{fmt}' is not supported.")
    return raw_bytes, ext


# -----------------------------------------------------------------------------
# Site Data & Static Bake Utilities
# -----------------------------------------------------------------------------
def validate_hex_color(value: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", (value or "").strip()))


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
        raw_buttons_json = config.get("buttons_json") or "[]"
        try:
            buttons = json.loads(raw_buttons_json)
        except (TypeError, ValueError):
            buttons = []
        fav_ext = config.get("favicon_ext", ".ico")
        logo_ext = config.get("org_logo_ext", ".png")
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

import os
import io
import json
import math
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as DefusedET
from svgelements import SVG

from config import ICON_SRC_URL, ICON_DIR

# SVG elements that are never allowed to survive sanitization, regardless of namespace.
# <script> is the direct code-execution vector; <foreignObject> can embed arbitrary
# non-SVG markup (including HTML <script>); <iframe> can load external content.
SVG_DISALLOWED_TAGS = {"script", "foreignObject", "iframe"}

# Attributes stripped from every surviving SVG element: any event handler (onload, onclick...)
# plus href/xlink:href, which can be used to reference and load external/remote content.
SVG_DISALLOWED_ATTR_PREFIXES = ("on",)
SVG_DISALLOWED_ATTRS = {"href", "xlink:href"}


def process_icon(icon_name, icon_data, source_catalog):
    """
    Process a single icon:
    1. Check aspect ratio filter (0.75 - 1.33).
    2. Sanitize SVG (remove scripts/event handlers).
    3. Calculate precise viewBox based on geometry using svgelements.
    4. Write to disk.

    Returns the icon_name if successful, None if filtered out.
    """
    source_width = icon_data.get("width", source_catalog.get("width", 24))
    source_height = icon_data.get("height", source_catalog.get("height", 24))
    body = icon_data.get("body", "")

    if not body:
        return None

    # Construct initial SVG wrapper
    xmlns = 'xmlns="http://www.w3.org/2000/svg"'
    if "xlink:href=" in body:
        xmlns += ' xmlns:xlink="http://w3.org"'

    try:
        body_root = DefusedET.fromstring(f"<svg {xmlns}>{body}</svg>")
    except Exception:
        return None

    root = ET.Element(
        "{http://www.w3.org/2000/svg}svg",
        {
            "width": str(source_width),
            "height": str(source_height),
            "viewBox": f"0 0 {source_width} {source_height}",
        },
    )
    root.extend(body_root)

    # Inline SVG Sanitization
    # This is an allowlist-by-removal approach: we remove the small set of constructs
    # that are unambiguously script-or-external-load vectors.
    def sanitize(el):
        for attr_name in list(el.attrib.keys()):
            local_attr = attr_name.split("}")[-1]
            if (
                local_attr.lower().startswith(SVG_DISALLOWED_ATTR_PREFIXES)
                or local_attr.lower() in SVG_DISALLOWED_ATTRS
            ):
                del el.attrib[attr_name]
        for child in list(el):
            local_tag = child.tag.split("}")[-1] if isinstance(child.tag, str) else ""
            if local_tag in SVG_DISALLOWED_TAGS:
                el.remove(child)
            else:
                sanitize(child)

    sanitize(root)
    ET.register_namespace("", "http://www.w3.org/2000/svg")

    # Parse for geometry calculation using svgelements
    svg_bytes = DefusedET.tostring(root, encoding="utf-8")
    try:
        svg_obj = SVG.parse(io.BytesIO(svg_bytes))

        # Calculate bounding box inline
        bbox = None
        for element in svg_obj.elements():
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
            except Exception:
                continue
            if element_bbox is None or not all(math.isfinite(v) for v in element_bbox):
                continue

            xmin, ymin, xmax, ymax = element_bbox
            if bbox is None:
                bbox = [xmin, ymin, xmax, ymax]
            else:
                bbox[0] = min(bbox[0], xmin)
                bbox[1] = min(bbox[1], ymin)
                bbox[2] = max(bbox[2], xmax)
                bbox[3] = max(bbox[3], ymax)

        if bbox is None:
            return None

    except Exception:
        return None

    xmin, ymin, xmax, ymax = bbox
    width = xmax - xmin
    height = ymax - ymin

    if width <= 0 or height <= 0:
        return None

    # FILTER: Aspect Ratio Check (0.75 to 1.33)
    # We discard icons that are too tall or too wide to ensure visual consistency
    # in the button layout.
    if not (0.75 <= (height / width) <= 1.33):
        return None

    # Update SVG attributes with precise bounds to remove excess whitespace
    root.set("viewBox", f"{xmin:g} {ymin:g} {width:g} {height:g}")
    root.set("width", f"{width:g}")
    root.set("height", f"{height:g}")

    # Write to disk
    os.makedirs(ICON_DIR, exist_ok=True)
    output_path = os.path.join(ICON_DIR, f"{icon_name}.svg")
    tmp_path = f"{output_path}.tmp"
    ET.ElementTree(root).write(tmp_path, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_path, output_path)

    return icon_name


def sync_and_process_icons():
    """Download the icon catalog, then filter, sanitize, and store the icons."""
    icon_files = list(Path(ICON_DIR).glob("*.svg", case_sensitive=False))
    if icon_files:
        # Icons exist on disk, no need to download anything
        print(f"Found {len(icon_files)} icons on disk, nothing to do.")
        return

    print("Downloading source catalog...")
    req = urllib.request.Request(
        ICON_SRC_URL, headers={"User-Agent": "Linkbuilder/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        source_payload = resp.read()

    source_catalog = json.loads(source_payload)
    if source_catalog.get("prefix") != "logos" or not isinstance(
        source_catalog.get("icons"), dict
    ):
        raise ValueError("Invalid source catalog format.")

    valid_icon_ids = []
    total = len(source_catalog["icons"])
    print(f"Processing {total} icons...")

    for i, (name, data) in enumerate(source_catalog["icons"].items()):
        if i % 100 == 0:
            print(f"  Progress: {i}/{total}")

        if process_icon(name, data, source_catalog):
            valid_icon_ids.append(name)

    print(f"Done! Synced and processed {len(valid_icon_ids)} valid icons.")


if __name__ == "__main__":
    try:
        sync_and_process_icons()
    except Exception as e:
        print(f"Error: {e}")
        exit(1)

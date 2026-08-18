#!/usr/bin/env python3
import os
import io
import json
import math
import urllib.request

import defusedxml.ElementTree as DefusedET
import xml.etree.ElementTree as ET
from svgelements import SVG

from common import (
    ICON_CATALOG_SRC_URL,
    CUSTOM_ICON_CATALOG_PATH,
    ICON_SVG_DIR,
    ICON_NAME_RE,
)

# SVG Sanitization Constants
SVG_DISALLOWED_TAGS = {"script", "foreignObject", "iframe"}
SVG_DISALLOWED_ATTR_PREFIXES = ("on",)
SVG_DISALLOWED_ATTRS = {"href", "xlink:href"}


def calculate_svg_bbox(svg):
    """Calculate bounding box of drawable SVG content."""
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
        except Exception:
            continue
        if element_bbox is None:
            continue
        if not all(math.isfinite(v) for v in element_bbox):
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
    return tuple(bbox)


def sanitize_svg_root(root):
    """Strip dangerous tags and attributes from an XML Element."""

    def strip_attrs(el):
        for attr_name in list(el.attrib.keys()):
            local_attr = attr_name.split("}")[-1]
            if (
                local_attr.lower().startswith(SVG_DISALLOWED_ATTR_PREFIXES)
                or local_attr.lower() in SVG_DISALLOWED_ATTRS
            ):
                del el.attrib[attr_name]

    def strip_recursive(el):
        strip_attrs(el)
        for child in list(el):
            local_tag = child.tag.split("}")[-1] if isinstance(child.tag, str) else ""
            if local_tag in SVG_DISALLOWED_TAGS:
                el.remove(child)
                continue
            strip_recursive(child)

    strip_recursive(root)
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return root


def process_icon(icon_name, icon_data, source_catalog):
    """
    Process a single icon:
    1. Check aspect ratio filter.
    2. Sanitize SVG.
    3. Calculate precise viewBox.
    4. Write to disk.
    Returns metadata dict if successful, None if filtered out.
    """
    source_width = icon_data.get("width", source_catalog.get("width", 24))
    source_height = icon_data.get("height", source_catalog.get("height", 24))
    body = icon_data.get("body", "")

    if not body:
        return None

    # Construct initial SVG
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

    # Sanitize immediately
    root = sanitize_svg_root(root)

    # Parse for geometry calculation
    svg_bytes = ET.tostring(root, encoding="utf-8")
    try:
        svg_obj = SVG.parse(io.BytesIO(svg_bytes))
        bbox = calculate_svg_bbox(svg_obj)
    except Exception:
        return None

    if bbox is None:
        return None

    xmin, ymin, xmax, ymax = bbox
    width = xmax - xmin
    height = ymax - ymin

    if width <= 0 or height <= 0:
        return None

    # FILTER: Aspect Ratio Check (0.75 to 1.33)
    aspect_ratio = height / width
    if not (0.75 <= aspect_ratio <= 1.33):
        return None

    # Update SVG attributes with precise bounds
    root.set("viewBox", f"{xmin:g} {ymin:g} {width:g} {height:g}")
    root.set("width", f"{width:g}")
    root.set("height", f"{height:g}")

    # Write to disk
    output_path = os.path.join(ICON_SVG_DIR, f"{icon_name}.svg")
    tmp_path = f"{output_path}.tmp"
    ET.ElementTree(root).write(tmp_path, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_path, output_path)

    return {
        "id": icon_name,
        "name": icon_name.replace("-", " ").title(),
        "width": float(width),
        "height": float(height),
    }


def sync_and_compile_icons():
    print("Downloading source catalog...")
    req = urllib.request.Request(
        ICON_CATALOG_SRC_URL, headers={"User-Agent": "Linkbuilder/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        source_payload = resp.read()

    source_catalog = json.loads(source_payload)
    if source_catalog.get("prefix") != "logos" or not isinstance(
        source_catalog.get("icons"), dict
    ):
        raise ValueError("Invalid source catalog format.")

    os.makedirs(ICON_SVG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(CUSTOM_ICON_CATALOG_PATH), exist_ok=True)

    custom_catalog = []
    total = len(source_catalog["icons"])
    print(f"Processing {total} icons...")

    for i, (name, data) in enumerate(source_catalog["icons"].items()):
        if i % 100 == 0:
            print(f"  Progress: {i}/{total}")

        meta = process_icon(name, data, source_catalog)
        if meta:
            custom_catalog.append(meta)

    # Sort by name for easier browsing in admin UI
    custom_catalog.sort(key=lambda x: x["name"].casefold())

    # Write custom catalog
    tmp_cat = f"{CUSTOM_ICON_CATALOG_PATH}.tmp"
    with open(tmp_cat, "w", encoding="utf-8") as f:
        json.dump({"icons": custom_catalog}, f, indent=2)
    os.replace(tmp_cat, CUSTOM_ICON_CATALOG_PATH)

    print(
        f"Done! Compiled {len(custom_catalog)} valid icons to {CUSTOM_ICON_CATALOG_PATH}"
    )


if __name__ == "__main__":
    try:
        sync_and_compile_icons()
    except Exception as e:
        print(f"Error: {e}")
        exit(1)

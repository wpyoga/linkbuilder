#!/usr/bin/env python3
from common import sync_icon_catalog

if __name__ == "__main__":
    try:
        catalog = sync_icon_catalog()
        print(f"SVG Logos catalog synchronized: {len(catalog['icons'])} icons.")
    except Exception as e:
        print(f"Error syncing icons: {e}")
        exit(1)

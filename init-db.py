#!/usr/bin/env python3
import os
import secrets
import sqlite3
from contextlib import contextmanager
from werkzeug.security import generate_password_hash

from config import DB_PATH, SECRET_KEY_FILE, DEPLOYMENT_SUPERADMIN_USER


@contextmanager
def get_db():
    """Context manager wrapper around SQLite database connection lifetime."""
    conn = sqlite3.connect(DB_PATH)
    # Enforce foreign key constraints inside SQLite session
    conn.execute("PRAGMA foreign_keys = ON;")
    # Set row factory to dictionary-like access for query outputs
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
    """Database initialization routine to set up tables and default admin account."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    # Generate and persist the secret key file if it doesn't already exist. We only
    # write it once -- if the file is already there, we leave it alone so existing
    # sessions aren't invalidated.
    if not os.path.exists(SECRET_KEY_FILE):
        secret_key = secrets.token_hex(32)  # 256-bit key, hex-encoded
        with open(SECRET_KEY_FILE, "w") as _f:
            _f.write(secret_key)
        os.chmod(SECRET_KEY_FILE, 0o600)  # owner read/write only
        print(f"Generated secret key file: {SECRET_KEY_FILE}")

    with get_db() as db:
        # Create users table for administrative access control
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_change_required INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Create site configuration table. Note: We store the icon catalog JSON here
        # under the key 'icon_catalog_json'. This allows the app to load the filtered
        # list of valid icons without parsing XML or hitting the network at runtime.
        db.execute("""
            CREATE TABLE IF NOT EXISTS site_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                blob_value BLOB
            )
        """)

        # Clear existing icon catalog from DB to force refresh on next sync/startup.
        # This ensures that if the filtering criteria in sync-icons.py changes, stale
        # icons don't persist in the admin UI.
        db.execute("DELETE FROM site_config WHERE key = 'icon_catalog_json'")
        print("Cleared existing icon catalog from database.")

        # Seed initial administrative user into the database if absent. If no password is
        # provided via SUPERADMIN_PASSWORD, generate a random one and print it once so the
        # operator can log in. We deliberately do NOT fall back to a static hardcoded
        # password: a static default shipped in source is a real credential-stuffing risk.
        existing = db.execute(
            "SELECT id FROM users WHERE username = ?", (DEPLOYMENT_SUPERADMIN_USER,)
        ).fetchone()

        if not existing:
            password = os.environ.get("SUPERADMIN_PASSWORD")
            generated = False
            if not password:
                # secrets.token_urlsafe gives a cryptographically random, URL-safe password.
                password = secrets.token_urlsafe(18)
                generated = True

            db.execute(
                "INSERT INTO users (username, password_hash, password_change_required) VALUES (?, ?, 1)",
                (
                    DEPLOYMENT_SUPERADMIN_USER,
                    generate_password_hash(password, method="scrypt"),
                ),
            )

            if generated:
                print("=" * 60)
                print(
                    "Generated superadmin password (shown once, not stored anywhere):"
                )
                print(f"  username: {DEPLOYMENT_SUPERADMIN_USER}")
                print(f"  password: {password}")
                print("=" * 60)


if __name__ == "__main__":
    init_db()
    print("Database initialization complete.")

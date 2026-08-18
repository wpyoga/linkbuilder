#!/usr/bin/env python3
import os
import secrets
from werkzeug.security import generate_password_hash
from common import get_db, SECRET_KEY_FILE, DEPLOYMENT_SUPERADMIN_USER


def init_db():
    os.makedirs(os.path.dirname(SECRET_KEY_FILE) or ".", exist_ok=True)

    if not os.path.exists(SECRET_KEY_FILE):
        secret_key = secrets.token_hex(32)
        with open(SECRET_KEY_FILE, "w") as _f:
            _f.write(secret_key)
        os.chmod(SECRET_KEY_FILE, 0o600)
        print(f"Generated secret key file: {SECRET_KEY_FILE}")

    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_change_required INTEGER NOT NULL DEFAULT 1
        )""")

        db.execute("""CREATE TABLE IF NOT EXISTS site_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            blob_value BLOB
        )""")

        existing = db.execute(
            "SELECT id FROM users WHERE username = ?", (DEPLOYMENT_SUPERADMIN_USER,)
        ).fetchone()

        if not existing:
            password = os.environ.get("SUPERADMIN_PASSWORD")
            generated = False
            if not password:
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
                print("Generated superadmin password (shown once):")
                print(f"  username: {DEPLOYMENT_SUPERADMIN_USER}")
                print(f"  password: {password}")
                print("=" * 60)


if __name__ == "__main__":
    init_db()
    print("Database initialization complete.")

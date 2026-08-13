import os
import json
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "change-this-to-a-secure-random-key"

DATA_FILE = "data.json"
# Target output directory served by Caddy
OUTPUT_DIR = "/srv/www/example.com/@info"


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "title": "Example Company",
        "bio": "Software Solutions",
        "vcard": {
            "fn": "John Doe",
            "org": "Example Inc.",
            "title": "Systems Engineer",
            "email": "john@example.com",
            "phone": "+1234567890",
            "url": "https://example.com",
        },
        "links": [
            {"label": "Official Website", "url": "https://example.com"},
            {"label": "Documentation", "url": "https://docs.example.com"},
        ],
    }


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def generate_vcard_content(vcard):
    """Generates standard vCard 3.0 text format."""
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


def bake_static_site(data):
    """Bakes HTML and physical .vcf file to the public static directory."""
    # Ensure target output directories exist
    vcard_dir = os.path.join(OUTPUT_DIR, "vcard")
    os.makedirs(vcard_dir, exist_ok=True)

    # 1. Write the static vCard file
    vcard_path = os.path.join(vcard_dir, "contact.vcf")
    vcard_content = generate_vcard_content(data.get("vcard", {}))
    with open(vcard_path, "w", encoding="utf-8") as f:
        f.write(vcard_content)

    # 2. Render and write index.html using relative paths
    rendered_html = render_template("site_template.html", data=data)
    index_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(rendered_html)


@app.route("/", methods=["GET"])
def admin():
    data = load_data()
    return render_template("admin.html", data=data)


@app.route("/save", methods=["POST"])
def save():
    title = request.form.get("title")
    bio = request.form.get("bio")

    # Extract vCard inputs
    vcard = {
        "fn": request.form.get("vcard_fn"),
        "org": request.form.get("vcard_org"),
        "title": request.form.get("vcard_title"),
        "email": request.form.get("vcard_email"),
        "phone": request.form.get("vcard_phone"),
        "url": request.form.get("vcard_url"),
    }

    # Extract dynamic links
    labels = request.form.getlist("link_label")
    urls = request.form.getlist("link_url")
    links = []
    for label, url in zip(labels, urls):
        if label.strip() and url.strip():
            links.append({"label": label.strip(), "url": url.strip()})

    data = {"title": title, "bio": bio, "vcard": vcard, "links": links}

    save_data(data)

    try:
        bake_static_site(data)
        flash("Site successfully baked to static HTML!", "success")
    except Exception as e:
        flash(f"Error generating static site: {str(e)}", "danger")

    return redirect(url_for("admin"))


if __name__ == "__main__":
    # Bind strictly to localhost/Tailscale interface, never expose port 5000 publicly
    app.run(host="127.0.0.1", port=5000, debug=True)

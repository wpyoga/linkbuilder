# linkbuilder

Vibe-coded selfhosted Linktree alternative.

General deployment strategy:
- Admin page deployed inside a VPN or similar internal network, for internal users only, or gated behind some kind of strong authentication service
- Static site generated as a directory containing plain HTML + assets, which can be served to the wide open Internet on another domain or even another server

This simplifies routing, reduces server load, and increases security.

## Development Server

Admin site:

```sh
./venv/bin/flask run
```

Public site:

```sh
python3 -m http.server --bind 127.0.0.1 -d pub
```

Replace `pub` with the output directory.

## Deployment

Simple manual deployment:

```sh
python3 -m venv venv
./venv/bin/pip3 install -r requirements.txt
DB_PATH="/var/lib/linkbuilder/data.db" \
OUTPUT_DIR="/var/www/html/mysite" \
SUPERADMIN_PASSWORD="MySecurePassword123!" \
( ./venv/bin/flask init-db
  ./venv/bin/python3 app.py )
```

Flask:

```sh
python3 -m venv venv
./venv/bin/pip3 install -r requirements.txt
DB_PATH="/var/lib/linkbuilder/data.db" \
OUTPUT_DIR="/var/www/html/mysite" \
SUPERADMIN_PASSWORD="MySecurePassword123!"; \
( ./venv/bin/flask init-db
  ./venv/bin/flask run )
```

Gunicorn:

```sh
python3 -m venv venv
./venv/bin/pip3 install -r requirements.txt
DB_PATH="/var/lib/linkbuilder/data.db" \
OUTPUT_DIR="/var/www/html/mysite" \
SUPERADMIN_PASSWORD="MySecurePassword123!" \
( ./venv/bin/flask init-db
  ./venv/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app:app )
```

Systemd:

```sh
python3 -m venv venv
./venv/bin/pip3 install -r requirements.txt
# customize deployment/linkbuilder.service and deployment/linkbuilder
cp deployment/linkbuilder.service /etc/systemd/system/
cp deployment/linkbuilder /etc/default/
systemctl enable --now linkbuilder.service
```

Docker:

```sh
# customize deployment/docker-compose.yaml
docker compose -f deployment/docker-compose.yaml
```

## Docker image

To build the image:

```sh
docker build -f deployment/Dockerfile -t linkbuilder .
```

### Development

This one-liner is useful for launching successive cached builds, with data volumes
wiped in between.

```sh
docker compose -f deployment/docker-compose.yaml -p linkbuilder up --build; docker compose -f deployment/docker-compose.yaml -p linkbuilder down
```

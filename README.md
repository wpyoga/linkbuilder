# linkbuilder

Vibe-coded selfhosted Linktree alternative.

General deployment strategy:

- Admin page deployed inside a VPN or similar internal network, for internal users only, or gated behind some kind of strong authentication service
- Static site generated as a directory containing plain HTML + assets, which can be served to the wide open Internet on another domain or even another server

This simplifies routing, reduces server load, and increases security.

## Development Server

Initialize application data:

```sh
$ ./venv/bin/python3 init-db.py
$ ./venv/bin/python3 sync-icons.py
```

Admin site:

```sh
$ ./venv/bin/flask run
```

Public site:

```sh
$ python3 -m http.server --bind 127.0.0.1 -d pub
```

Replace `pub` with the output directory.

## Deployment

### Prerequisites

- Python 3.8+
- A directory for the database (e.g., /var/lib/linkbuilder)
- A directory for the public site output (e.g., /var/www/html/mysite)

### Manual Deployment

1. Set up environment:

```sh
python3 -m venv venv
./venv/bin/pip3 install -r requirements.txt
```

2. Configure environment variables:

```sh
export DB_PATH="/var/lib/linkbuilder/data.db"
export OUTPUT_DIR="/srv/www/example.com/@info"
export SUPERADMIN_PASSWORD="MySecurePassword123!"
```

3. Initialize app data:

```sh
./venv/bin/python3 init-db.py
./venv/bin/python3 sync-icons.py
```

4. Deployment options:

Simple manual deployment:

```sh
./venv/bin/python3 app.py
```

Flask:

```sh
./venv/bin/flask run
```

Gunicorn:

```sh
./venv/bin/gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app:app
```

Systemd:

```sh
# customize deployment/linkbuilder.service and deployment/linkbuilder
cp deployment/linkbuilder.service /etc/systemd/system/
cp deployment/linkbuilder /etc/default/
systemctl enable --now linkbuilder.service
```

Docker:

No need to initialize environment for Docker deployment.

```sh
# customize deployment/docker-compose.yaml
docker compose -f deployment/docker-compose.yaml
```

## Docker image

To build the image without deploying anything:

```sh
docker build -f deployment/Dockerfile -t linkbuilder .
```

### Development

This one-liner is useful for launching successive cached builds, with data volumes
wiped in between.

```sh
docker compose -f deployment/docker-compose.yaml -p linkbuilder up --build; docker compose -f deployment/docker-compose.yaml -p linkbuilder down -v
```

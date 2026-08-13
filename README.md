# linkbuilder

Vibe-coded selfhosted Linktree alternative.

## Deployment

Simple manual deployment:

```sh
python3 -m venv venv
./venv/bin/pip3 install -r requirements.txt
DB_PATH="/var/lib/linkbuilder/data.db" \
OUTPUT_DIR="/var/www/html/mysite" \
SUPERADMIN_PASSWORD="MySecurePassword123!" \
python3 app.py
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

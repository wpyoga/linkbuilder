#!/bin/sh

# Exit immediately if any command fails
set -e

echo "Initializing application data"
python3 init-db.py

echo "Starting application"
# Use exec to run this command, so that it replaces this script as PID 1
# So that it will receive signals and can gracefully shut down
exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 app:app


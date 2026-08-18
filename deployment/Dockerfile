FROM python:3.14-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Maximize the use of Docker layer caching
# Work on seldom-changed outputs first

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entrypoint script
COPY deployment/entrypoint.sh /

# Copy application source code
COPY templates templates
COPY app.py .

# Create a non-root user for security
# These commands are instant, so just combine everything in one RUN directive
# The chown commands needs to be run after copying app.py anyway
RUN useradd -m appuser \
    && mkdir /app/data \
    && mkdir /app/pub \
    && chown -R appuser:appuser /app \
    && chmod +x /entrypoint.sh

# Switch to non-root user for security
USER appuser

# Prepare button icons
RUN flask sync-icons

# Run Gunicorn binding to all interfaces on port 8000
CMD ["/entrypoint.sh"]

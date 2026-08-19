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

# Copy our favicon
RUN mkdir static
COPY static/linkbuilder.svg static

# Entrypoint script
COPY deployment/entrypoint.sh .

# Copy application source code
COPY templates templates
COPY app.py .
COPY config.py .
COPY init-db.py .
COPY sync-icons.py .

# Create a non-root user for security
# These commands are instant, so just combine everything in one RUN directive
# The chown commands needs to be run after copying app.py anyway
RUN useradd -m appuser \
    && mkdir /app/data \
    && mkdir /app/pub \
    && chmod +x /app/entrypoint.sh \
    && chown -R appuser:appuser /app

# Switch to non-root user for security
USER appuser

# Prepare button icons
RUN python3 sync-icons.py

# Run Gunicorn binding to all interfaces on port 8000
ENTRYPOINT ["/app/entrypoint.sh"]

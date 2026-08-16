FROM python:3.14-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY app.py .
COPY templates templates

# Create a non-root user for security
RUN useradd -m appuser \
    && mkdir /app/data \
    && mkdir /app/pub \
    && chown -R appuser:appuser /app

# Switch to non-root user for security
# This can be overridden with the Docker Compose user: directive
USER appuser

# Run Gunicorn binding to all interfaces on port 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "app:app"]

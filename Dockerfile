FROM python:3.10-slim

# System deps needed by opencv, insightface, pillow-heif, mysql client
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libheif1 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Directories the app writes to (embeddings, uploads, logs) — mounted as
# volumes in docker-compose so they persist across container restarts
RUN mkdir -p dataset/images dataset/embeddings uploads

EXPOSE 5000

# gunicorn: 2 workers x 2 threads is a sane starting point for a
# t3.medium/t3.large; tune WEB_CONCURRENCY via env if needed.
# The recognition streams run in background threads inside the app
# itself (see recognition/stream_manager.py) so we keep --threads on.
CMD ["gunicorn", "-b", "0.0.0.0:5000", \
     "--workers", "2", "--threads", "4", "--timeout", "120", \
     "app:create_app()"]

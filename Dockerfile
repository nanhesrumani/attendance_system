# attendance_system/Dockerfile
FROM python:3.10-slim

# System deps for OpenCV, InsightFace, mysqlclient
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1-mesa-glx \
    libgomp1 \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Create dirs that the app writes to at runtime
RUN mkdir -p static/uploads/attendance_sheets dataset/embeddings

# Expose Flask port
EXPOSE 5000

# Use run.py as entrypoint
CMD ["python", "run.py"]
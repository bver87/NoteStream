# ============================================================
#  NoteStream – Dockerfile
#  Single-stage build for maximum compatibility.
#  Includes build tools for compiled packages (argon2, etc.)
#  and pre-downloads the Whisper model at build time.
# ============================================================

FROM python:3.11-slim

# ffmpeg        — audio conversion
# build-essential — needed to compile argon2-cffi and other C extensions
# curl          — used by Docker healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Pre-download Whisper model at build time ----
# Stored in /app/models — bind-mounted in docker-compose so it
# persists across rebuilds and does not re-download every time.
ARG WHISPER_MODEL=large-v3
ENV WHISPER_MODEL=${WHISPER_MODEL}
ENV HF_HOME=/app/models
ENV TRANSFORMERS_CACHE=/app/models

RUN python -c "\
from faster_whisper import WhisperModel; \
import os; \
m = os.environ.get('WHISPER_MODEL', 'large-v3'); \
print(f'Pre-downloading Whisper model: {m} ...'); \
WhisperModel(m, device='cpu', compute_type='int8'); \
print('Model ready.')"

# ---- App source (last — maximises layer cache) ----
COPY app ./app

# ---- Runtime dirs ----
RUN mkdir -p /app/uploads /app/output /data

# ---- Environment ----
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV MAX_WORKERS=2

# IMPORTANT: set in docker-compose / Portainer env vars:
#   SESSION_SECRET  — secret key for signed session cookies
#   DB_PATH         — path to SQLite file (default /data/app.db)
#   RESEND_API_KEY  — for password reset emails
#   APP_BASE_URL    — your public URL e.g. http://192.168.1.x:8001

EXPOSE 8000

# --workers 1: Whisper is CPU-bound, multiple workers = multiple models in RAM
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
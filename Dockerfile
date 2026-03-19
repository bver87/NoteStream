# ============================================================
#  NoteStream – Dockerfile
#  Multi-stage build: keeps the final image lean while
#  pre-downloading the Whisper model at build time so the
#  first transcription job doesn't stall on a cold start.
# ============================================================

# ---- Stage 1: build dependencies -------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile some Python packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- Stage 2: runtime image ------------------------------
FROM python:3.11-slim

# ffmpeg for audio conversion; no git needed at runtime
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only the installed packages from the builder stage
COPY --from=builder /install /usr/local

# ---- Pre-download Whisper model at build time ----
# Model is stored in /app/models so it survives container restarts
# when /app is a named volume. Override WHISPER_MODEL to use a
# smaller model (e.g. medium) without rebuilding your app code.
ARG WHISPER_MODEL=large-v3
ENV WHISPER_MODEL=${WHISPER_MODEL}
ENV HF_HOME=/app/models
ENV TRANSFORMERS_CACHE=/app/models

RUN python -c "\
from faster_whisper import WhisperModel; \
import os; \
m = os.environ.get('WHISPER_MODEL', 'large-v3'); \
print(f'Pre-downloading Whisper model: {m}'); \
WhisperModel(m, device='cpu', compute_type='int8'); \
print('Model download complete.')"

# ---- App source (copied last — maximises layer cache) ----
COPY app ./app

# ---- Runtime dirs ----
RUN mkdir -p /app/uploads /app/output /data

# ---- Environment ----
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Number of parallel transcription workers (default 2)
ENV MAX_WORKERS=2

# IMPORTANT: set these in your docker-compose / deployment:
#   SESSION_SECRET  – secret key for signed session cookies
#   DB_PATH         – path to SQLite file (default /data/app.db)
#   RESEND_API_KEY  – for password reset emails

# ---- Expose & start ----
EXPOSE 8000

# Use --workers 1: transcription is CPU-bound; multiple uvicorn
# workers would each load the full Whisper model into RAM.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
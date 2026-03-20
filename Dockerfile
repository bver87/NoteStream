# ============================================================
#  NoteStream – Dockerfile
# ============================================================

FROM python:3.11-slim

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        build-essential \
        curl \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip, setuptools and wheel first — fixes many install failures
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Pre-download Whisper model at build time ----
ARG WHISPER_MODEL=medium
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

# ---- App source ----
COPY app ./app

# ---- Runtime dirs ----
RUN mkdir -p /app/uploads /app/output /data

# ---- Environment ----
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV MAX_WORKERS=2

EXPOSE 8000

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
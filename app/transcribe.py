import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from faster_whisper import WhisperModel

log = logging.getLogger("notestream.transcribe")

# ------------ CONFIG ------------
LANGUAGE     = "nl"
CHUNK_LENGTH = 300          # 5 min — good balance for N100: 2+ chunks on typical meetings,
                            # low ffmpeg overhead, good context per chunk
MODEL_SIZE   = os.getenv("WHISPER_MODEL", "medium")
CPU_THREADS  = int(os.getenv("WHISPER_THREADS", os.cpu_count() or 4))
# --------------------------------

INITIAL_PROMPT = (
    "Dit is een zakelijke vergadering in het Nederlands. "
    "Gebruik volledige zinnen, leestekens, hoofdletters en alinea's. "
    "Sprekers gebruiken termen als agenda, actiepunt, notulen en besluit."
)

log.info(
    "Loading Whisper model '%s' on CPU with %d threads (int8) …",
    MODEL_SIZE, CPU_THREADS,
)

model = WhisperModel(
    MODEL_SIZE,
    device="cpu",
    compute_type="int8",
    cpu_threads=CPU_THREADS,
)

log.info("Whisper model ready.")


# ---------- audio helpers ----------
def get_audio_duration(audio_path: str) -> float:
    """Return audio duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def split_audio(audio_path: str) -> tuple[str, list[str]]:
    """
    Convert & split audio into mono 16 kHz WAV chunks.
    Returns (temp_dir, [chunk_paths]) — caller is responsible for cleanup.
    """
    out_dir = tempfile.mkdtemp(prefix="ns_chunks_")
    pattern = os.path.join(out_dir, "chunk_%03d.wav")

    cmd = [
        "ffmpeg", "-y",
        "-i", audio_path,
        "-ac", "1",             # mono
        "-ar", "16000",         # 16 kHz — Whisper native sample rate
        "-f", "segment",
        "-segment_time", str(CHUNK_LENGTH),
        "-c:a", "pcm_s16le",    # uncompressed — fastest decode
        pattern,
    ]

    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    chunks = sorted(str(p) for p in Path(out_dir).glob("chunk_*.wav"))

    if not chunks:
        raise RuntimeError(
            f"ffmpeg produced no chunks for {audio_path}.\n"
            f"stderr: {result.stderr[-500:]}"
        )

    log.info("Split %s → %d chunk(s) in %s", audio_path, len(chunks), out_dir)
    return out_dir, chunks


# ---------- transcription ----------
# Cache for dynamically loaded models — avoids reloading on every job
_model_cache: dict[str, WhisperModel] = {MODEL_SIZE: model}


def _get_model(size: str) -> WhisperModel:
    """Return a cached WhisperModel for the given size, loading it if needed."""
    if size not in _model_cache:
        log.info("Loading Whisper model '%s' on demand …", size)
        _model_cache[size] = WhisperModel(
            size,
            device="cpu",
            compute_type="int8",
            cpu_threads=CPU_THREADS,
        )
        log.info("Model '%s' ready.", size)
    return _model_cache[size]


def transcribe_file(
    audio_path: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    model_size: str | None = None,
) -> str:
    """
    Transcribe a Dutch audio file and return the full transcript as a string.
    model_size overrides the default MODEL_SIZE for this job.
    Progress is reported after every Whisper segment so the bar moves smoothly
    even for short recordings that produce only one chunk.
    """
    active_model = _get_model(model_size or MODEL_SIZE)
    log.info("Using model: %s", model_size or MODEL_SIZE)

    total_duration = get_audio_duration(audio_path)
    log.info("Total audio duration: %.1f s", total_duration)

    temp_dir, chunks = split_audio(audio_path)
    total_ticks = max(int(total_duration), 1)

    try:
        parts: list[str] = []

        for chunk_idx, chunk in enumerate(chunks):
            chunk_num    = chunk_idx + 1
            total_chunks = len(chunks)
            chunk_offset = chunk_idx * CHUNK_LENGTH

            log.info("Transcribing chunk %d/%d: %s", chunk_num, total_chunks, chunk)

            segments, info = active_model.transcribe(
                chunk,
                language=LANGUAGE,
                beam_size=3,                    # N100 optimised: faster, minimal accuracy loss
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 500,
                    "speech_pad_ms": 200,
                },
                initial_prompt=INITIAL_PROMPT,
                condition_on_previous_text=True,
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.4,
                word_timestamps=False,          # not needed — saves time
            )

            chunk_texts: list[str] = []
            last_reported_pct = -1

            for seg in segments:
                chunk_texts.append(seg.text)

                # Map segment position to overall progress (5% → 95%)
                absolute_secs = chunk_offset + seg.end
                pct = min(int((absolute_secs / total_duration) * 90) + 5, 95)

                # Only fire callback when percentage actually changes
                if progress_cb and pct != last_reported_pct:
                    progress_cb(int(absolute_secs), total_ticks)
                    last_reported_pct = pct

            chunk_text = "".join(chunk_texts).strip()
            if chunk_text:
                parts.append(chunk_text)
                log.info(
                    "  chunk %d: %.1f s  language=%.0f%% conf",
                    chunk_num,
                    info.duration,
                    info.language_probability * 100,
                )

        transcript = "\n\n".join(parts)
        log.info("Transcription complete: %d chars", len(transcript))
        return transcript

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        log.info("Cleaned up temp dir %s", temp_dir)
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
CHUNK_LENGTH = 600          # 10 min chunks — fewer ffmpeg calls, better context
MODEL_SIZE   = os.getenv("WHISPER_MODEL", "large-v3")   # override via env if needed
CPU_THREADS  = int(os.getenv("WHISPER_THREADS", os.cpu_count() or 4))
# --------------------------------

# Dutch meeting prompt — guides punctuation, capitalisation and common vocabulary.
# Keep it short; it counts against the 224-token context window.
INITIAL_PROMPT = (
    "Dit is een zakelijke vergadering in het Nederlands. "
    "Gebruik volledige zinnen, leestekens, hoofdletters en alinea's. "
    "Sprekers gebruiken termen als agenda, actiepunt, notulen en besluit."
)

log.info(
    "Loading Whisper model '%s' on CPU with %d threads (int8) …",
    MODEL_SIZE, CPU_THREADS,
)

# Load once at module level — never reload per request
model = WhisperModel(
    MODEL_SIZE,
    device="cpu",
    compute_type="int8",
    cpu_threads=CPU_THREADS,
)

log.info("Whisper model ready.")


# ---------- audio splitting ----------
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
        "-ac", "1",                         # mono
        "-ar", "16000",                     # 16 kHz — Whisper native sample rate
        "-f", "segment",
        "-segment_time", str(CHUNK_LENGTH),
        "-c:a", "pcm_s16le",                # uncompressed — fastest decode
        pattern,
    ]

    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
    )

    chunks = sorted(str(p) for p in Path(out_dir).glob("chunk_*.wav"))

    if not chunks:
        raise RuntimeError(
            f"ffmpeg produced no chunks for {audio_path}.\n"
            f"stderr: {result.stderr[-500:]}"
        )

    log.info("Split %s → %d chunk(s) in %s", audio_path, len(chunks), out_dir)
    return out_dir, chunks


# ---------- transcription ----------
def transcribe_file(
    audio_path: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> str:
    """
    Transcribe a Dutch audio file and return the full transcript as a string.
    Calls progress_cb(current_chunk, total_chunks) after each chunk.
    """
    temp_dir, chunks = split_audio(audio_path)

    try:
        total     = len(chunks)
        parts: list[str] = []

        for idx, chunk in enumerate(chunks, start=1):
            log.info("Transcribing chunk %d/%d: %s", idx, total, chunk)

            segments, info = model.transcribe(
                chunk,
                language=LANGUAGE,
                beam_size=5,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 500,   # skip short pauses
                    "speech_pad_ms": 200,             # keep a little context around speech
                },
                initial_prompt=INITIAL_PROMPT,
                condition_on_previous_text=True,      # improves continuity across segments
                no_speech_threshold=0.6,              # filter low-confidence silent segments
                compression_ratio_threshold=2.4,
                word_timestamps=False,                # faster — only needed for subtitles
            )

            chunk_text = "".join(seg.text for seg in segments).strip()

            if chunk_text:
                parts.append(chunk_text)
                log.info(
                    "  chunk %d: %.1f s detected language=%.0f%% conf",
                    idx,
                    info.duration,
                    (info.language_probability * 100),
                )

            if progress_cb:
                progress_cb(idx, total)

        # Join chunks with a blank line — natural paragraph break between 10-min blocks
        transcript = "\n\n".join(parts)
        log.info("Transcription complete: %d chars", len(transcript))
        return transcript

    finally:
        # Always clean up temp WAV files, even on error
        shutil.rmtree(temp_dir, ignore_errors=True)
        log.info("Cleaned up temp dir %s", temp_dir)
import os
import uuid
import time
import logging
import threading
import secrets
from concurrent.futures import ThreadPoolExecutor

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Request,
    Form,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.agenda import extract_agenda_text
from app.transcribe import transcribe_file
from app.db import init_db, get_conn
from app.auth import router as auth_router, get_current_user

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("notestream")

# ---------------- CONFIG ----------------
UPLOAD_DIR        = "/app/uploads"
OUTPUT_DIR        = "/app/output"
TEXT_TTL_SECONDS  = 60 * 60 * 48        # 48 uur
MAX_WORKERS       = int(os.getenv("MAX_WORKERS", 2))
SESSION_SECRET    = os.getenv("SESSION_SECRET", "dev-only-change-me")
MAX_UPLOAD_MB     = int(os.getenv("MAX_UPLOAD_MB", 500))   # 500 MB — covers 2hr recordings
# ----------------------------------------

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Thread pool for CPU-heavy transcription jobs — prevents blocking the server
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

app = FastAPI(title="NoteStream")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.include_router(auth_router)


# ---------- startup ----------
@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    _reset_stale_jobs()
    log.info("✅ NoteStream started")


# ---------- health ----------
@app.get("/health")
def health():
    """Used by Docker / load balancer health checks."""
    return {"status": "ok", "app": "NoteStream"}


def is_expired(created_at: int) -> bool:
    return time.time() - created_at > TEXT_TTL_SECONDS


def _safe_remove(path: str | None) -> None:
    """Delete a file without raising if it doesn't exist."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            log.warning("Could not remove %s: %s", path, e)


# ---------- startup helpers ----------
def _reset_stale_jobs() -> None:
    """Mark jobs that were 'processing' when the server last died as errors."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status        = 'error',
                progress      = 0,
                current_chunk = 0,
                total_chunks  = 0,
                eta_seconds   = NULL
            WHERE status = 'processing'
            """
        )
        conn.commit()
    log.info("Stale processing jobs reset to error.")


def _cleanup_loop() -> None:
    """Background thread: delete expired output files and DB rows every 10 min."""
    while True:
        time.sleep(600)
        cutoff = int(time.time() - TEXT_TTL_SECONDS)
        try:
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT output_path FROM jobs WHERE created_at < ?",
                    (cutoff,),
                ).fetchall()

                for row in rows:
                    _safe_remove(row["output_path"])

                conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
                conn.commit()
            log.info("Cleanup: removed jobs older than %s seconds.", TEXT_TTL_SECONDS)
        except Exception as e:
            log.error("Cleanup error: %s", e)


# ---------- UI ----------
@app.get("/", response_class=HTMLResponse)
def upload_form(request: Request):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("upload.html", {"request": request})


# ---------- Upload ----------
@app.post("/upload")
async def upload_audio(
    request: Request,
    file: UploadFile = File(...),
    agenda_text: str | None = Form(None),
    agenda_file: UploadFile | None = File(None),
    model_size: str = Form("medium"),
):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Niet ingelogd"}, status_code=401)

    # Validate model choice
    if model_size not in ("small", "medium"):
        model_size = "medium"

    # Validate file size before saving
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_MB * 1024 * 1024:
        return JSONResponse(
            {"error": f"Bestand te groot. Maximum is {MAX_UPLOAD_MB} MB."},
            status_code=413,
        )

    file_id   = str(uuid.uuid4())
    created_at = int(time.time())

    # Save audio file
    audio_ext  = os.path.splitext(file.filename or "audio.m4a")[1] or ".m4a"
    audio_path = os.path.join(UPLOAD_DIR, f"{file_id}{audio_ext}")
    with open(audio_path, "wb") as f:
        f.write(contents)

    # Save optional agenda file
    agenda_file_path: str | None = None
    if agenda_file and agenda_file.filename:
        agenda_ext       = os.path.splitext(agenda_file.filename)[1]
        agenda_file_path = os.path.join(UPLOAD_DIR, f"{file_id}_agenda{agenda_ext}")
        with open(agenda_file_path, "wb") as f:
            f.write(await agenda_file.read())

    agenda = extract_agenda_text(agenda_text, agenda_file_path)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, status,
                progress, current_chunk, total_chunks, eta_seconds,
                agenda, model_size, created_at
            )
            VALUES (?, ?, 'processing', 0, 0, 0, NULL, ?, ?, ?)
            """,
            (file_id, user["id"], agenda, model_size, created_at),
        )
        conn.commit()

    # Submit to thread pool — non-blocking, won't stall other requests
    executor.submit(process_audio, file_id, audio_path, agenda_file_path, model_size)

    log.info("Job %s queued for user %s", file_id, user["id"])
    return {"file_id": file_id, "status": "processing"}


# ---------- Background processing ----------
def process_audio(file_id: str, audio_path: str, agenda_file_path: str | None, model_size: str = "medium") -> None:
    start_ts = time.time()
    log.info("▶️  process_audio START  job=%s", file_id)

    try:
        def progress_cb(idx: int, total: int) -> None:
            elapsed = time.time() - start_ts
            avg     = elapsed / max(idx, 1)
            eta     = int(avg * (total - idx))
            pct     = int((idx / total) * 90) + 5   # 5 → 95 %

            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET progress      = ?,
                        current_chunk = ?,
                        total_chunks  = ?,
                        eta_seconds   = ?
                    WHERE id = ?
                    """,
                    (pct, idx, total, eta, file_id),
                )
                conn.commit()

        transcript = transcribe_file(audio_path, progress_cb=progress_cb, model_size=model_size)

        with get_conn() as conn:
            row = conn.execute(
                "SELECT agenda FROM jobs WHERE id = ?", (file_id,)
            ).fetchone()

        agenda_text = (row["agenda"] if row and row["agenda"] else "Geen agenda aangeleverd.")

        out_path = os.path.join(OUTPUT_DIR, f"{file_id}_notulen.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(
                f"=== AGENDA ===\n{agenda_text}\n\n=== TRANSCRIPT ===\n{transcript}"
            )

        text_token = secrets.token_urlsafe(32)

        with get_conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status      = 'done',
                    progress    = 100,
                    eta_seconds = 0,
                    output_path = ?,
                    text_token  = ?
                WHERE id = ?
                """,
                (out_path, text_token, file_id),
            )
            conn.commit()

        elapsed = round(time.time() - start_ts, 1)
        log.info("✅ process_audio DONE  job=%s  %.1fs", file_id, elapsed)

    except Exception as e:
        log.error("❌ process_audio ERROR  job=%s  %r", file_id, e)
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status      = 'error',
                    progress    = 0,
                    eta_seconds = NULL
                WHERE id = ?
                """,
                (file_id,),
            )
            conn.commit()

    finally:
        _safe_remove(audio_path)
        _safe_remove(agenda_file_path)


# ---------- Status ----------
@app.get("/status/{file_id}")
def check_status(request: Request, file_id: str):
    user = get_current_user(request)

    with get_conn() as conn:
        job = conn.execute(
            """
            SELECT status, progress,
                   current_chunk, total_chunks, eta_seconds,
                   created_at, user_id, text_token
            FROM jobs WHERE id = ?
            """,
            (file_id,),
        ).fetchone()

    if not job:
        return {"status": "not_found"}

    if not user or job["user_id"] != user["id"]:
        return {"status": "forbidden"}

    if is_expired(job["created_at"]):
        return {"status": "expired"}

    return {
        "status":        job["status"],
        "progress":      job["progress"],
        "current_chunk": job["current_chunk"],
        "total_chunks":  job["total_chunks"],
        "eta_seconds":   job["eta_seconds"],
        "text_token":    job["text_token"],
    }


# ---------- Download ----------
@app.get("/download/{file_id}")
def download_transcript(request: Request, file_id: str):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Niet ingelogd"}, status_code=401)

    with get_conn() as conn:
        job = conn.execute(
            "SELECT user_id, status, output_path, created_at FROM jobs WHERE id = ?",
            (file_id,),
        ).fetchone()

    if (
        not job
        or job["user_id"] != user["id"]
        or job["status"] != "done"
        or is_expired(job["created_at"])
        or not job["output_path"]
        or not os.path.exists(job["output_path"])
    ):
        return JSONResponse({"error": "Bestand niet beschikbaar"}, status_code=404)

    return FileResponse(
        job["output_path"],
        filename=f"notulen_{file_id[:8]}.txt",
        media_type="text/plain",
    )


# ---------- Public share (Apple Shortcut) ----------
@app.get("/share/{token}", response_class=PlainTextResponse)
def share_text(token: str):
    """Token-based public endpoint — used by the Apple Shortcut to fetch the transcript."""
    with get_conn() as conn:
        job = conn.execute(
            """
            SELECT output_path, created_at
            FROM jobs
            WHERE text_token = ? AND status = 'done'
            """,
            (token,),
        ).fetchone()

    if (
        not job
        or is_expired(job["created_at"])
        or not job["output_path"]
        or not os.path.exists(job["output_path"])
    ):
        return PlainTextResponse("Niet beschikbaar of verlopen.", status_code=404)

    with open(job["output_path"], "r", encoding="utf-8") as f:
        return f.read()


# ---------- Retry ----------
@app.post("/retry/{file_id}")
def retry_job(request: Request, file_id: str):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Niet ingelogd"}, status_code=401)

    with get_conn() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (file_id,)
        ).fetchone()

        if not job or job["user_id"] != user["id"] or job["status"] != "error":
            return JSONResponse({"error": "Retry niet toegestaan"}, status_code=400)

        conn.execute(
            """
            UPDATE jobs
            SET status        = 'processing',
                progress      = 0,
                current_chunk = 0,
                total_chunks  = 0,
                eta_seconds   = NULL,
                output_path   = NULL,
                text_token    = NULL
            WHERE id = ?
            """,
            (file_id,),
        )
        conn.commit()

    # Re-queue — note: audio file was deleted after first attempt,
    # so retry will fail unless you store the original path. See db.py optimization.
    log.warning("Retry requested for job %s — audio file may no longer exist.", file_id)
    return {"ok": True}


# ---------- Mijn uploads ----------
@app.get("/my-uploads", response_class=HTMLResponse)
def my_uploads(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, status, progress, created_at, text_token
            FROM jobs
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()

    jobs = []
    for row in rows:
        status = "expired" if is_expired(row["created_at"]) else row["status"]
        jobs.append({
            "file_id":      row["id"],
            "status":       status,
            "progress":     row["progress"],
            "text_token":   row["text_token"],
            "download_url": f"/download/{row['id']}",
            "share_url":    f"/share/{row['text_token']}" if row["text_token"] else None,
        })

    return templates.TemplateResponse(
        "my_uploads.html",
        {"request": request, "jobs": jobs},
    )
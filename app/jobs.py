import time
from app.db import get_conn


# ---------- CREATE ----------
def create_job(file_id: str, user_id: str, agenda: str | None):
    now = int(time.time())

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id,
                user_id,
                status,
                agenda,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                file_id,
                user_id,
                "processing",
                agenda,
                now,
            ),
        )
        conn.commit()


# ---------- READ ----------
def get_job(file_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (file_id,),
        ).fetchone()


def get_jobs_for_user(user_id: str):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()


def get_processing_jobs():
    """
    Voor resume na crash:
    haal alle jobs op die nog 'processing' zijn
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = 'processing'
            """
        ).fetchall()


# ---------- UPDATE ----------
def update_job_status(
    file_id: str,
    status: str,
    output_path: str | None = None,
):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET
                status = ?,
                output_path = COALESCE(?, output_path)
            WHERE id = ?
            """,
            (status, output_path, file_id),
        )
        conn.commit()


def mark_job_error(file_id: str, error: str | None = None):
    """
    Bewust simpel gehouden.
    Eventueel later uitbreiden met error-kolom.
    """
    update_job_status(file_id, "error")


# ---------- DELETE ----------
def delete_job(file_id: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM jobs WHERE id = ?",
            (file_id,),
        )
        conn.commit()
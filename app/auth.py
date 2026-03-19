import hashlib
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from app.mail import send_password_reset
from app.db import get_conn

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext

log = logging.getLogger("notestream.auth")

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Argon2 is the strongest password hashing scheme available in passlib
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

EMAIL_RE        = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESET_TOKEN_TTL = 60 * 30   # 30 minutes
MIN_PASSWORD_LEN = 8


# ---------- helpers ----------
def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_token(token: str) -> str:
    """One-way SHA-256 hash for storing reset tokens — never store raw tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow_iso() -> str:
    """Return current UTC time as ISO-8601 string (timezone-aware)."""
    return datetime.now(timezone.utc).isoformat()


# ---------- user helpers ----------
def create_user(email: str, password: str) -> dict:
    email = _normalize_email(email)

    if not EMAIL_RE.match(email):
        raise ValueError("Ongeldig e-mailadres.")

    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Wachtwoord moet minimaal {MIN_PASSWORD_LEN} tekens zijn.")

    user_id       = str(uuid.uuid4())
    password_hash = pwd_context.hash(password)
    created_at    = _utcnow_iso()

    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO users (id, email, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, email, password_hash, created_at),
            )
            conn.commit()
        except Exception:
            # Unique constraint on email — don't leak whether the email exists
            raise ValueError("Dit e-mailadres is al geregistreerd.")

    log.info("New user registered: %s", email)
    return {"id": user_id, "email": email}


def authenticate(email: str, password: str) -> dict | None:
    email = _normalize_email(email)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    # Always run verify even on missing user to prevent timing attacks
    dummy_hash = pwd_context.hash("dummy") if not row else None
    hash_to_check = row["password_hash"] if row else dummy_hash

    if not pwd_context.verify(password, hash_to_check) or not row:
        log.warning("Failed login attempt for email: %s", email)
        return None

    log.info("User authenticated: %s", email)
    return {"id": row["id"], "email": row["email"]}


def get_current_user(request: Request) -> dict | None:
    """Return the logged-in user dict from the session, or None."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        # Session references a deleted user — clear it
        request.session.clear()
        return None

    return {"id": row["id"], "email": row["email"]}


# ---------- password reset ----------
def create_password_reset(email: str) -> str | None:
    email = _normalize_email(email)

    with get_conn() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()

        # No info leak — return None silently if email not found
        if not user:
            return None

        raw_token  = secrets.token_urlsafe(32)
        token_hash = _hash_token(raw_token)
        now        = int(time.time())

        # Invalidate any existing unused tokens for this user
        conn.execute(
            """
            UPDATE password_resets
            SET used_at = ?
            WHERE user_id = ? AND used_at IS NULL
            """,
            (now, user["id"]),
        )

        conn.execute(
            """
            INSERT INTO password_resets
                (id, user_id, token_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), user["id"], token_hash, now + RESET_TOKEN_TTL, now),
        )
        conn.commit()

    log.info("Password reset token created for user_id=%s", user["id"])
    return raw_token


def reset_password(token: str, new_password: str) -> bool:
    if not new_password or len(new_password) < MIN_PASSWORD_LEN:
        return False

    token_hash = _hash_token(token)
    now        = int(time.time())

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, user_id FROM password_resets
            WHERE token_hash = ?
              AND used_at    IS NULL
              AND expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()

        if not row:
            return False

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (pwd_context.hash(new_password), row["user_id"]),
        )
        conn.execute(
            "UPDATE password_resets SET used_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()

    log.info("Password reset completed for user_id=%s", row["user_id"])
    return True


def validate_reset_token(token: str) -> bool:
    token_hash = _hash_token(token)
    now        = int(time.time())

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM password_resets
            WHERE token_hash = ?
              AND used_at    IS NULL
              AND expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()

    return bool(row)


# ---------- auth routes ----------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None}
    )


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = authenticate(email, password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "E-mail of wachtwoord klopt niet."},
            status_code=400,
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "register.html", {"request": request, "error": None}
    )


@router.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        user = create_user(email, password)
    except ValueError as e:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": str(e)},
            status_code=400,
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- password reset routes ----------
@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(
        "forgot_password.html", {"request": request, "message": None}
    )


@router.post("/forgot")
def forgot_submit(request: Request, email: str = Form(...)):
    token = create_password_reset(email)
    if token:
        send_password_reset(email, token)
    # Always show the same message — no info leak on whether email exists
    return templates.TemplateResponse(
        "forgot_password.html",
        {
            "request": request,
            "message": "Als dit e-mailadres bestaat, ontvang je een reset-link.",
        },
    )


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(request: Request, token: str):
    if not validate_reset_token(token):
        return HTMLResponse("Reset-link is ongeldig of verlopen.", status_code=400)
    return templates.TemplateResponse(
        "reset_password.html",
        {"request": request, "token": token, "error": None},
    )


@router.post("/reset/{token}")
def reset_submit(request: Request, token: str, password: str = Form(...)):
    ok = reset_password(token, password)
    if not ok:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "error": "Link ongeldig of verlopen."},
            status_code=400,
        )
    return RedirectResponse("/login", status_code=303)
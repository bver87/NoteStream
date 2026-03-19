import logging
import os

import resend

log = logging.getLogger("notestream.mail")

APP_BASE_URL   = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
FROM_ADDRESS   = os.getenv("MAIL_FROM", "NoteStream <noreply@notestream.app>")

# Assigned once at import — resend reads this as a module-level global
resend.api_key = os.getenv("RESEND_API_KEY", "")


def send_password_reset(email: str, token: str) -> None:
    """Send a password-reset e-mail via Resend. Logs and swallows errors so
    a mail failure never crashes the auth flow — the user just won't get an email."""

    if not resend.api_key:
        log.error("RESEND_API_KEY is not set — password reset email not sent to %s", email)
        return

    reset_link = f"{APP_BASE_URL}/reset/{token}"

    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#060a12;font-family:'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 16px;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#0c1220;border:1px solid #1a2540;border-radius:14px;overflow:hidden;">

          <!-- Header -->
          <tr>
            <td style="padding:28px 32px 20px;border-bottom:1px solid #1a2540;">
              <span style="font-size:20px;font-weight:800;letter-spacing:-.5px;
                           background:linear-gradient(90deg,#22d3ee,#818cf8);
                           -webkit-background-clip:text;-webkit-text-fill-color:transparent;
                           background-clip:text;">
                NoteStream
              </span>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:28px 32px;color:#e2e8f0;">
              <p style="margin:0 0 14px;font-size:16px;font-weight:600;">
                Wachtwoord opnieuw instellen
              </p>
              <p style="margin:0 0 20px;font-size:14px;color:#94a3b8;line-height:1.6;">
                Je hebt een wachtwoord-reset aangevraagd voor je NoteStream account.
                Klik op de knop hieronder om een nieuw wachtwoord in te stellen.
                De link is <strong style="color:#e2e8f0;">30 minuten</strong> geldig.
              </p>

              <!-- CTA button -->
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="border-radius:10px;
                             background:linear-gradient(135deg,#22d3ee,#38bdf8);">
                    <a href="{reset_link}"
                       style="display:inline-block;padding:12px 24px;
                              color:#060a12;font-size:14px;font-weight:700;
                              text-decoration:none;border-radius:10px;">
                      Wachtwoord resetten →
                    </a>
                  </td>
                </tr>
              </table>

              <p style="margin:20px 0 0;font-size:12px;color:#64748b;line-height:1.5;">
                Werkt de knop niet? Kopieer deze link in je browser:<br>
                <a href="{reset_link}" style="color:#22d3ee;word-break:break-all;">
                  {reset_link}
                </a>
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:16px 32px;border-top:1px solid #1a2540;
                       font-size:12px;color:#64748b;line-height:1.5;">
              Heb je dit niet aangevraagd? Dan kun je deze mail veilig negeren.<br>
              Je wachtwoord blijft ongewijzigd.
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    try:
        resend.Emails.send({
            "from":    FROM_ADDRESS,
            "to":      email,
            "subject": "Wachtwoord resetten – NoteStream",
            "html":    html,
        })
        log.info("Password reset email sent to %s", email)
    except Exception as e:
        log.error("Failed to send password reset email to %s: %r", email, e)
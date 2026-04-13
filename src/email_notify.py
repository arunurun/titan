"""Optional SMTP notification after a successful live audit (same post text as Supabase)."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _smtp_config() -> dict[str, object] | None:
    host = os.environ.get("SMTP_HOST", "").strip()
    raw_to = os.environ.get("EMAIL_TO", "").strip()
    from_addr = os.environ.get("EMAIL_FROM", "").strip()
    if not host or not raw_to or not from_addr:
        return None
    to_addrs = [x.strip() for x in raw_to.split(",") if x.strip()]
    if not to_addrs:
        return None
    user = os.environ.get("SMTP_USER", "").strip()
    # Gmail app passwords are often pasted as "xxxx xxxx xxxx xxxx"; SMTP expects no spaces.
    password = os.environ.get("SMTP_PASSWORD", "").strip().replace(" ", "")
    if user and not password:
        logger.warning("SMTP_USER is set but SMTP_PASSWORD is empty; skipping email.")
        return None
    raw_port = os.environ.get("SMTP_PORT", "").strip()
    port = int(raw_port) if raw_port else 587
    use_tls_raw = (os.environ.get("SMTP_USE_TLS") or "true").strip()
    use_tls = use_tls_raw.lower() in ("1", "true", "yes")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from": from_addr,
        "to": to_addrs,
        "use_tls": use_tls,
    }


def send_success_post_email(post_text: str, *, subject_prefix: str = "Titan V12.0 audit") -> bool:
    """
    Send plain-text email with the narrative post (matches what we store in Supabase).
    No-op if SMTP_HOST / EMAIL_FROM / EMAIL_TO are not all set.

    Env: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO (comma-separated),
         SMTP_USE_TLS (default true). For port 465, set SMTP_USE_TLS=false and use SMTP_SSL behavior via port.
    """
    cfg = _smtp_config()
    if not cfg:
        logger.info("Email notify skipped (set SMTP_HOST, EMAIL_FROM, EMAIL_TO, and password if required).")
        return False

    host = str(cfg["host"])
    port = int(cfg["port"])
    user = str(cfg["user"])
    password = str(cfg["password"])
    from_addr = str(cfg["from"])
    to_list: list[str] = cfg["to"]  # type: ignore[assignment]
    use_tls = bool(cfg["use_tls"])

    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    subject = f"{subject_prefix} — {stamp}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    msg.set_content(post_text.strip(), subtype="plain", charset="utf-8")

    try:
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except OSError as e:
        logger.warning("SMTP connection failed (network or host): %s", e)
        return False
    except smtplib.SMTPException as e:
        logger.warning("SMTP send failed: %s", e)
        return False

    logger.info("Sent audit email to %s", to_list)
    return True


def send_failure_email(
    summary_line: str,
    *,
    detail: str = "",
    subject_prefix: str = "Titan V12.0 audit",
) -> bool:
    """
    Notify on live run failure (same SMTP env as success email). Subject includes summary_line
    so inbox/GH notifications show Breeze vs Supabase vs Gemini without opening the log.
    """
    cfg = _smtp_config()
    if not cfg:
        logger.info("Failure email skipped (SMTP not configured).")
        return False

    host = str(cfg["host"])
    port = int(cfg["port"])
    user = str(cfg["user"])
    password = str(cfg["password"])
    from_addr = str(cfg["from"])
    to_list: list[str] = cfg["to"]  # type: ignore[assignment]
    use_tls = bool(cfg["use_tls"])

    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    subj = f"{subject_prefix} FAILED — {summary_line.strip()}"
    if len(subj) > 200:
        subj = subj[:197] + "..."
    body = summary_line.strip()
    if detail.strip():
        body = f"{body}\n\n--- Details ---\n{detail.strip()}"

    msg = EmailMessage()
    msg["Subject"] = subj
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    msg.set_content(
        f"Titan live audit failed at {stamp}.\n\n{body}",
        subtype="plain",
        charset="utf-8",
    )

    try:
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as smtp:
                smtp.ehlo()
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except OSError as e:
        logger.warning("Failure email: SMTP connection failed: %s", e)
        return False
    except smtplib.SMTPException as e:
        logger.warning("Failure email: SMTP send failed: %s", e)
        return False

    logger.info("Sent failure notification to %s", to_list)
    return True

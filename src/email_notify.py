"""Optional SMTP notification after a successful live audit (same post text as Supabase)."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime
from html import escape
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


def _render_success_html(post_text: str, *, subject: str) -> str:
    lines = [ln.rstrip() for ln in (post_text or "").splitlines()]
    current_section = "Overview"
    sections: dict[str, list[str]] = {current_section: []}
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("--- ") and text.endswith(" ---"):
            current_section = text.strip("- ").strip()
            sections.setdefault(current_section, [])
            continue
        sections.setdefault(current_section, []).append(text)

    palette = ["#4285f4", "#ea4335", "#fbbc05", "#34a853"]

    def card(title: str, inner: str, *, color: str) -> str:
        return (
            '<div style="border:1px solid #e0e3e7;border-top:4px solid '
            f'{color};border-radius:12px;padding:12px;margin:0 0 12px;background:#fff;">'
            f'<h3 style="margin:0 0 10px;font-size:16px;color:{color};">{escape(title)}</h3>{inner}</div>'
        )

    blocks: list[str] = []
    for idx, (name, items) in enumerate(sections.items()):
        if not items:
            continue
        color = palette[idx % len(palette)]
        if name.lower() == "per-symbol metrics":
            rows: list[str] = []
            for item in items:
                if item.startswith("--- ") and item.endswith(" ---"):
                    continue
                if "|" in item:
                    cols = [escape(x.strip()) for x in item.split("|")]
                    symbol = cols[0] if cols else ""
                    facts = "<br>".join(cols[1:])
                    rows.append(
                        "<tr>"
                        f'<td style="padding:8px;border-bottom:1px solid #eee;vertical-align:top;font-weight:600;">{symbol}</td>'
                        f'<td style="padding:8px;border-bottom:1px solid #eee;vertical-align:top;font-family:monospace;font-size:12px;">{facts}</td>'
                        "</tr>"
                    )
                else:
                    rows.append(
                        "<tr>"
                        '<td colspan="2" style="padding:8px;border-bottom:1px solid #eee;">'
                        f"{escape(item)}</td></tr>"
                    )
            table = (
                '<table style="width:100%;border-collapse:collapse;">'
                "<thead><tr>"
                '<th style="text-align:left;padding:8px;border-bottom:2px solid #ddd;">Symbol</th>'
                '<th style="text-align:left;padding:8px;border-bottom:2px solid #ddd;">Metrics</th>'
                "</tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
            blocks.append(card(name, table, color=color))
            continue

        list_html = "".join(
            f'<li style="margin:0 0 6px;">{escape(item)}</li>'
            for item in items
        )
        blocks.append(
            card(name, f'<ul style="margin:0;padding-left:18px;">{list_html}</ul>', color=color)
        )

    return (
        "<html><body style=\"margin:0;padding:16px;background:#f8f9fa;color:#202124;font-family:Arial,sans-serif;\">"
        f'<h2 style="margin:0 0 6px;">{escape(subject)}</h2>'
        '<div style="height:4px;border-radius:999px;margin:0 0 12px;background:linear-gradient(90deg,#4285f4 0 25%,#ea4335 25% 50%,#fbbc05 50% 75%,#34a853 75% 100%);"></div>'
        f"{''.join(blocks)}"
        '<p style="color:#5f6368;font-size:12px;margin-top:12px;">Generated by Titan V12.0</p>'
        "</body></html>"
    )


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
    msg.add_alternative(_render_success_html(post_text, subject=subject), subtype="html")

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

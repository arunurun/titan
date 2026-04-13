from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests


def send_webhook_alert(message: str) -> None:
    url = (os.environ.get("ALERT_WEBHOOK_URL") or "").strip()
    if not url:
        return
    requests.post(url, json={"text": message}, timeout=15).raise_for_status()


def send_email_alert(subject: str, body: str) -> None:
    host = (os.environ.get("SMTP_HOST") or "").strip()
    port = int((os.environ.get("SMTP_PORT") or "0").strip() or "0")
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    sender = (os.environ.get("EMAIL_FROM") or "").strip()
    recipient = (os.environ.get("EMAIL_TO") or "").strip()

    if not all([host, port, user, password, sender, recipient]):
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)

"""Minimal SMTP sender. Defaults target the in-cluster Mailpit catcher;
point the SMTP_* env vars at a real provider for production email."""
import os
import smtplib
from email.message import EmailMessage


def send_email(to: str, subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "mailpit-smtp.fileapp.svc.cluster.local")
    port = int(os.environ.get("SMTP_PORT", "1025"))
    sender = os.environ.get("SMTP_FROM", "no-reply@files.local")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    starttls = os.environ.get("SMTP_STARTTLS", "false").lower() == "true"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=10) as s:
        if starttls:
            s.starttls()
        if user:
            s.login(user, password)
        s.send_message(msg)

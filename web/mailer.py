"""Minimal SMTP sender shared by the verification-email and report-email
features. Configured entirely via TRADINGAGENTS_SMTP_* environment
variables — HOST/USER/PASS required, PORT (default 587) and FROM
(default: same as USER) optional.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


class MailerNotConfigured(RuntimeError):
    """Raised when TRADINGAGENTS_SMTP_HOST/USER/PASS aren't all set."""


def is_configured() -> bool:
    return bool(
        os.environ.get("TRADINGAGENTS_SMTP_HOST")
        and os.environ.get("TRADINGAGENTS_SMTP_USER")
        and os.environ.get("TRADINGAGENTS_SMTP_PASS")
    )


def send_email(to: str, subject: str, html_body: str) -> None:
    """Send one HTML email. Raises MailerNotConfigured if the SMTP env vars
    aren't set, or the underlying smtplib exception on send failure —
    callers decide how to surface each to their caller."""
    smtp_host = os.environ.get("TRADINGAGENTS_SMTP_HOST")
    smtp_user = os.environ.get("TRADINGAGENTS_SMTP_USER")
    smtp_pass = os.environ.get("TRADINGAGENTS_SMTP_PASS")
    if not smtp_host or not smtp_user or not smtp_pass:
        raise MailerNotConfigured(
            "邮件发送服务未配置。请联系系统管理员在服务器端配置 "
            "TRADINGAGENTS_SMTP_HOST, TRADINGAGENTS_SMTP_USER, TRADINGAGENTS_SMTP_PASS 环境变量。"
        )
    smtp_port = int(os.environ.get("TRADINGAGENTS_SMTP_PORT", "587"))
    smtp_from = os.environ.get("TRADINGAGENTS_SMTP_FROM") or smtp_user

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    if smtp_port == 465:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10.0)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=10.0)
        server.ehlo()
        server.starttls()
        server.ehlo()
    try:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_from, to, msg.as_string())
    finally:
        server.quit()

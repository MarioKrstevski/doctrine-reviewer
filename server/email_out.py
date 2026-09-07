"""Outbound email: deliberately not implemented.

Thank-you notes are queued in the notifications table and sent by hand
from /outbox. When a mail sender is wanted, fill in send() -- everything
else already exists. Until then SEND_EMAIL stays off, and turning it on
without SMTP_URL refuses to boot rather than silently dropping mail.
"""


def check_config(cfg):
    """Called at startup. Raises if sending is enabled but cannot work."""
    if cfg.send_email and not cfg.smtp_url:
        raise RuntimeError(
            "SEND_EMAIL is on but SMTP_URL is not set. Either set SMTP_URL "
            "(and implement email_out.send) or unset SEND_EMAIL.")


def send(notification, cfg) -> bool:
    """Send one notification. Returns True only if it was actually sent."""
    if not cfg.send_email:
        return False
    raise NotImplementedError(
        "email_out.send(): no sender is implemented. Thank-yous are sent by "
        "hand from /outbox until one is.")

"""Send operator alerts without blocking the agent event loop."""

import asyncio
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass(frozen=True)
class GmailMailer:
    sender: str
    recipient: str
    password: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "GmailMailer":
        keys = ("GMAIL_EMAIL", "ALERT_EMAIL_TO", "GMAIL_APP_PASSWORD")
        sender, recipient, password = (os.getenv(key, "").strip() for key in keys)
        missing = [key for key, value in zip(keys, (sender, recipient, password)) if not value]
        if missing:
            raise ValueError(f"Missing Gmail alert settings: {', '.join(missing)}")
        return cls(sender, recipient, "".join(password.split()))

    async def send(self, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send, subject, body)

    def _send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(body)
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=10, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(self.sender, self.password)
            refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)

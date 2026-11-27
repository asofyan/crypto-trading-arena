import smtplib
import ssl
from unittest.mock import MagicMock

import pytest

from arena.alerts import GmailMailer


@pytest.mark.parametrize("refused", [{}, {"recipient@example.com": (550, b"rejected")}])
async def test_gmail_sends_over_verified_tls(monkeypatch, refused):
    monkeypatch.setenv("GMAIL_EMAIL", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "aaaa bbbb cccc dddd")
    monkeypatch.setenv("ALERT_EMAIL_TO", "recipient@example.com")
    server = MagicMock()
    server.send_message.return_value = refused
    connection = MagicMock()
    connection.__enter__.return_value = server
    smtp = MagicMock(return_value=connection)
    monkeypatch.setattr("arena.alerts.smtplib.SMTP_SSL", smtp)

    mailer = GmailMailer.from_env()
    if refused:
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            await mailer.send("subject", "body")
    else:
        await mailer.send("subject", "body")

    assert smtp.call_args.args == ("smtp.gmail.com", 465)
    assert smtp.call_args.kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    server.login.assert_called_once_with("sender@example.com", "aaaabbbbccccdddd")
    message = server.send_message.call_args.args[0]
    assert message["To"] == "recipient@example.com"
    assert message["Subject"] == "subject"
    assert message.get_content().strip() == "body"


def test_gmail_requires_configuration(monkeypatch):
    for key in ("GMAIL_EMAIL", "GMAIL_APP_PASSWORD", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="GMAIL_EMAIL"):
        GmailMailer.from_env()

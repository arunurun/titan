"""email_notify optional SMTP."""

from unittest.mock import MagicMock, patch

from email_notify import send_failure_email, send_success_post_email


def test_send_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("EMAIL_TO", raising=False)
    assert send_success_post_email("hello") is False
    assert send_failure_email("[Supabase] test") is False


def test_send_calls_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "a@example.com,b@example.com")

    mock_smtp = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_smtp)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with patch("email_notify.smtplib.SMTP", return_value=mock_ctx):
        assert send_success_post_email("Post body") is True
    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once_with("u", "p")
    mock_smtp.send_message.assert_called_once()
    sent = mock_smtp.send_message.call_args[0][0]
    assert sent.get_content().strip() == "Post body"


def test_send_ssl_465(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("EMAIL_FROM", "f@example.com")
    monkeypatch.setenv("EMAIL_TO", "t@example.com")

    mock_smtp = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_smtp)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with patch("email_notify.smtplib.SMTP_SSL", return_value=mock_ctx):
        assert send_success_post_email("x") is True
    mock_smtp.login.assert_called_once()
    mock_smtp.send_message.assert_called_once()


def test_send_failure_email_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "a@example.com")

    mock_smtp = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_smtp)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    with patch("email_notify.smtplib.SMTP", return_value=mock_ctx):
        assert send_failure_email("[Breeze] token expired", detail="Traceback...") is True
    mock_smtp.send_message.assert_called_once()
    sent = mock_smtp.send_message.call_args[0][0]
    assert "FAILED" in sent["Subject"]
    assert "[Breeze]" in sent["Subject"]
    assert "Traceback" in sent.get_content()

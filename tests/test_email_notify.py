"""email_notify optional SMTP."""

from unittest.mock import MagicMock, patch

from email_notify import _render_success_html, send_action_required_email, send_failure_email, send_success_post_email


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
    assert sent.get_body(preferencelist=("plain",)).get_content().strip() == "Post body"
    assert sent.get_body(preferencelist=("html",)) is not None


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


def test_send_action_required_email_has_clickable_link(monkeypatch):
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
        assert (
            send_action_required_email(
                "Token invalid",
                action_url="https://api.icicidirect.com/apiuser/login?api_key=test",
                action_label="Login to Breeze",
                detail="Reason: expired token",
            )
            is True
        )
    sent = mock_smtp.send_message.call_args[0][0]
    plain = sent.get_body(preferencelist=("plain",)).get_content()
    html = sent.get_body(preferencelist=("html",)).get_content()
    assert "Login to Breeze" in plain
    assert "https://api.icicidirect.com/apiuser/login?api_key=test" in plain
    assert 'href="https://api.icicidirect.com/apiuser/login?api_key=test"' in html


def test_success_html_portfolio_per_symbol_uses_full_width_columns():
    body = (
        "--- Per-symbol metrics ---\n"
        "Legends: Book % = share\n"
        "SYMBOL | Titan | Curr ₹ | Book % | Unrl % | Tape | Intent | NextWk | Risk | Drivers\n"
        "RELIANCE | HOLD | ₹1.20 L | 10.0% | +10.5% | 1d +0.2% · z +0.10 | 55.0 | 72.0 | 4.5 | nextWeek soft\n"
        "* rollup footnote\n"
    )
    html = _render_success_html(body, subject="Titan test")
    assert html.count("<th ") == 10
    assert "RELIANCE" in html and "72.0" in html and "Tape" in html
    assert "rollup footnote" in html


def test_success_html_sector_per_symbol_metrics_use_cards():
    body = (
        "--- Per-symbol metrics ---\n"
        "MTARTECH (NSE) — Cut risk (exit or size down)\n"
        "Scores · next week 54.0 · intent 50.0 (balanced / neutral) · 1d -1.0%\n"
        "Tape · z 0.0 (near mean) · volume thin participation · ATR 2.0%\n"
        "IDEAFORGE (NSE) — Hold\n"
        "Scores · next week 60.0 · intent 55.0 (moderate long bias) · 1d +0.1%\n"
    )
    html = _render_success_html(body, subject="Titan sector")
    assert html.count("border-radius:10px;padding:12px 14px") == 2
    assert "MTARTECH (NSE)" in html and "IDEAFORGE (NSE)" in html
    assert "Scores ·" in html

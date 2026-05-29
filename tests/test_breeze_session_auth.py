import pytest

from breeze_session_auth import (
    build_breeze_login_url,
    parse_api_session_from_input,
    upsert_env_var,
    validate_breeze_session_token,
)


def test_build_breeze_login_url_encodes_key():
    url = build_breeze_login_url("key+with/special")
    assert "api_key=key%2Bwith%2Fspecial" in url or "api_key=" in url
    assert url.startswith("https://api.icicidirect.com/apiuser/login")


def test_parse_api_session_raw():
    assert parse_api_session_from_input("  abc123xyz  ") == "abc123xyz"


def test_parse_api_session_from_url():
    u = "https://127.0.0.1:9080/callback?API_Session=tokenValue&x=1"
    assert parse_api_session_from_input(u) == "tokenValue"


def test_parse_api_session_icici_lowercase_apisession():
    u = "http://127.0.0.1:9080/?apisession=55142575"
    assert parse_api_session_from_input(u) == "55142575"


def test_parse_api_session_empty_raises():
    with pytest.raises(ValueError):
        parse_api_session_from_input("")


def test_upsert_env_var(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=1\n", encoding="utf-8")
    upsert_env_var(p, "BREEZE_SESSION_TOKEN", "newtok")
    text = p.read_text(encoding="utf-8")
    assert "BREEZE_SESSION_TOKEN=newtok" in text
    assert "FOO=1" in text


def test_validate_breeze_session_token_rejects_empty():
    with pytest.raises(ValueError, match="required"):
        validate_breeze_session_token("   ")


def test_validate_breeze_session_token_rejects_wrapped_quotes():
    with pytest.raises(ValueError, match="wrapped in quotes"):
        validate_breeze_session_token('"abc1234567"')


def test_validate_breeze_session_token_rejects_newline():
    with pytest.raises(ValueError, match="single line"):
        validate_breeze_session_token("abc\n1234567")


def test_validate_breeze_session_token_rejects_short():
    with pytest.raises(ValueError, match="too short"):
        validate_breeze_session_token("1234567")


def test_validate_breeze_session_token_accepts_valid():
    assert validate_breeze_session_token("   abc1234567   ") == "abc1234567"

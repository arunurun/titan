import pytest
import requests
import time
import db.session_config as sc

pytestmark = pytest.mark.integration


def test_persist_token_creates_api_call(server):
    """Test that /api/token/persist accepts a token, calls Supabase, and updates session_config."""
    base = server
    token_value = "test_api_session_12345"

    # Persist token via API
    r = requests.post(
        f"{base}/api/token/persist",
        json={"token_input": token_value, "also_write_env": False},
        timeout=120
    )
    assert r.status_code in (200, 400), f"Unexpected status {r.status_code}: {r.text}"
    j = r.json()
    assert "ok" in j

    # If persistence succeeded, verify Supabase row
    if j.get("ok"):
        for _ in range(10):
            try:
                stored = sc.get_breeze_token()
                if stored == token_value:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        stored = sc.get_breeze_token()
        assert stored == token_value, f"Expected {token_value}, got {stored}"


def test_validate_token_endpoint(server):
    """Test that /api/token/validate can be called and returns ok/status."""
    base = server
    r = requests.post(
        f"{base}/api/token/validate",
        json={},
        timeout=120
    )
    assert r.status_code == 200
    j = r.json()
    assert "ok" in j
    assert "status" in j


def test_analysis_run_endpoint(server):
    """Test that /api/analysis/run accepts a request and returns a run_id."""
    base = server
    r = requests.post(
        f"{base}/api/analysis/run",
        json={"mode": "sector", "test": True},
        timeout=120
    )
    assert r.status_code == 200
    j = r.json()
    assert "run_id" in j
    run_id = j["run_id"]

    # Poll the run status
    for _ in range(30):
        r2 = requests.get(f"{base}/api/analysis/run/{run_id}", timeout=120)
        assert r2.status_code == 200
        j2 = r2.json()
        if j2.get("status") in ("completed", "failed"):
            break
        time.sleep(0.5)

    # Verify final status
    j_final = r2.json()
    assert j_final.get("status") in ("completed", "failed")


def test_health_endpoint(server):
    """Test that /api/health returns ok."""
    base = server
    r = requests.get(f"{base}/api/health", timeout=120)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True


def test_sectors_endpoint(server):
    """Test that /api/sectors returns a list of sectors."""
    base = server
    r = requests.get(f"{base}/api/sectors", timeout=120)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert isinstance(j.get("sectors"), list)

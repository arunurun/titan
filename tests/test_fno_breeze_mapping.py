"""Validate config/fno_breeze_mapping.yaml against NSE fo_mktlots and sector allowlists."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from breeze_client import (
    clear_fno_breeze_mapping_cache_for_tests,
    fetch_option_metrics_with_expiry_fallback,
    load_fno_breeze_mapping,
    nfo_underlying_code_candidates,
)
from config_loader import TitanConfig
from options_context import load_fno_symbols

ROOT = Path(__file__).resolve().parent.parent
FO_CACHE = ROOT / "data" / "cache" / "fo_mktlots.csv"
SECTOR_DIR = ROOT / "data" / "sector_allowlists"
INDEX_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})


@pytest.fixture(autouse=True)
def _clear_mapping_cache():
    clear_fno_breeze_mapping_cache_for_tests()
    yield
    clear_fno_breeze_mapping_cache_for_tests()


def _load_fo_mktlots_symbols() -> set[str]:
    assert FO_CACHE.is_file(), f"Missing cached NSE file: {FO_CACHE}"
    text = FO_CACHE.read_text(encoding="utf-8", errors="replace")
    out: set[str] = set()
    reader = csv.DictReader(io.StringIO(text))
    assert reader.fieldnames, "fo_mktlots.csv has no headers"
    reader.fieldnames = [f.strip() for f in reader.fieldnames]
    for row in reader:
        sym = (row.get("SYMBOL") or "").strip().upper()
        if sym and sym not in INDEX_UNDERLYINGS:
            out.add(sym)
    assert out, "fo_mktlots.csv parsed zero stock underlyings"
    return out


def _load_sector_symbols() -> set[str]:
    syms: set[str] = set()
    for path in SECTOR_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for s in data.get("symbols", []):
            syms.add(str(s).strip().upper())
    return syms


def _sector_fno_symbols() -> set[str]:
    return _load_sector_symbols() & _load_fo_mktlots_symbols()


def test_mapping_file_has_entry_for_every_sector_fno_symbol():
    mapping = load_fno_breeze_mapping()
    sector_fno = _sector_fno_symbols()
    missing = sorted(sector_fno - set(mapping))
    assert not missing, f"sector F&O symbols missing from mapping: {missing}"
    assert len(sector_fno) == 142


def test_every_mapping_entry_is_in_fo_mktlots():
    mapping = load_fno_breeze_mapping()
    fo_syms = _load_fo_mktlots_symbols()
    fno_yaml = load_fno_symbols()
    extras = sorted(set(mapping) - fo_syms)
    assert not extras, f"mapping keys not in fo_mktlots: {extras}"
    assert set(mapping) == fno_yaml


def test_no_mapping_entry_for_confirmed_non_fno_sector_symbols():
    mapping = load_fno_breeze_mapping()
    sector = _load_sector_symbols()
    fo_syms = _load_fo_mktlots_symbols()
    non_fno = sector - fo_syms
    leaked = sorted(non_fno & set(mapping))
    assert not leaked, f"non-F&O sector symbols in mapping: {leaked}"


def test_non_fno_sector_symbols_not_in_mapping():
    mapping = load_fno_breeze_mapping()
    non_fno = _load_sector_symbols() - _load_fo_mktlots_symbols()
    assert non_fno, "expected non-F&O sector symbols"
    assert not (non_fno & set(mapping))


def test_sample_mappings():
    mapping = load_fno_breeze_mapping()
    assert mapping["BEL"] == "BHAELE"
    assert mapping["INDUSTOWER"] == "BHAINF"
    assert mapping["HAL"] == "HINAER"
    assert mapping["BHARTIARTL"] == "BHAAIR"


def test_nfo_underlying_code_candidates_uses_explicit_mapping_first():
    codes = nfo_underlying_code_candidates("BEL")
    assert codes[0] == "BHAELE"
    assert "BEL" in codes


def test_nfo_underlying_code_candidates_industower_mapping_first():
    codes = nfo_underlying_code_candidates("INDUSTOWER")
    assert codes[0] == "BHAINF"


@patch("breeze_client.fetch_option_metrics_for_underlying")
def test_fetch_option_metrics_uses_mapping_code(mock_fetch):
    mock_fetch.return_value = {
        "underlying": "BEL",
        "call_oi": 100.0,
        "put_oi": 200.0,
        "chain_df": __import__("pandas").DataFrame({"strike": [500.0], "oi": [100.0]}),
        "call_chain_df": __import__("pandas").DataFrame({"strike": [500.0], "oi": [100.0]}),
        "put_chain_df": __import__("pandas").DataFrame({"strike": [490.0], "oi": [200.0]}),
        "expiry_date": "2026-06-30T06:00:00.000Z",
    }
    breeze = MagicMock()
    result = fetch_option_metrics_with_expiry_fallback(breeze, "BEL", max_expiry_tries=1)
    assert result.get("option_chain_unavailable") is not True
    assert result["nfo_stock_code"] == "BHAELE"
    assert mock_fetch.call_args[0][1] == "BHAELE"


def _breeze_credentials_present() -> bool:
    return all(
        (os.environ.get(k) or "").strip()
        for k in ("BREEZE_API_KEY", "BREEZE_SECRET", "BREEZE_SESSION_TOKEN")
    )


@pytest.mark.integration
@pytest.mark.breeze_live
@pytest.mark.skipif(not _breeze_credentials_present(), reason="Breeze credentials not configured")
def test_live_option_chain_sample_symbols():
    """Optional live check: mapped codes return Success (not HTTP 500). Rate-limit friendly subset."""
    from breeze_client import (
        create_breeze_session,
        fetch_option_metrics_for_underlying,
        iter_monthly_expiry_candidates,
    )

    cfg = TitanConfig(
        breeze_api_key=os.environ["BREEZE_API_KEY"],
        breeze_secret=os.environ["BREEZE_SECRET"],
        breeze_session_token=os.environ["BREEZE_SESSION_TOKEN"],
        gemini_api_keys=tuple(),
        supabase_url="",
        supabase_key="",
    )
    breeze = create_breeze_session(cfg)
    mapping = load_fno_breeze_mapping()
    sample = ["BEL", "INDUSTOWER", "HAL", "BHARTIARTL"]
    expiry = iter_monthly_expiry_candidates(months_ahead=1)[0]
    for sym in sample:
        code = mapping[sym]
        metrics = fetch_option_metrics_for_underlying(breeze, code, expiry)
        assert metrics["call_oi"] >= 0.0
        assert metrics["put_oi"] >= 0.0


@pytest.mark.integration
@pytest.mark.breeze_live
@pytest.mark.skipif(not _breeze_credentials_present(), reason="Breeze credentials not configured")
def test_live_option_chain_all_mapped_sector_fno():
    """Optional nightly-style check for all sector F&O mappings (skipped without credentials)."""
    from breeze_client import (
        create_breeze_session,
        fetch_option_metrics_for_underlying,
        iter_monthly_expiry_candidates,
    )

    cfg = TitanConfig(
        breeze_api_key=os.environ["BREEZE_API_KEY"],
        breeze_secret=os.environ["BREEZE_SECRET"],
        breeze_session_token=os.environ["BREEZE_SESSION_TOKEN"],
        gemini_api_keys=tuple(),
        supabase_url="",
        supabase_key="",
    )
    breeze = create_breeze_session(cfg)
    mapping = load_fno_breeze_mapping()
    expiry = iter_monthly_expiry_candidates(months_ahead=1)[0]
    sector_fno = sorted(_sector_fno_symbols())
    failures: list[str] = []
    for sym in sector_fno:
        code = mapping[sym]
        try:
            metrics = fetch_option_metrics_for_underlying(breeze, code, expiry)
            if metrics["call_oi"] == 0.0 and metrics["put_oi"] == 0.0:
                failures.append(f"{sym}->{code}: zero OI")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{sym}->{code}: {exc}")
    assert not failures, "live option chain failures:\n" + "\n".join(failures[:20])

"""Tests for options chain context helpers."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from options_context import (
    build_options_audit_fields,
    build_sector_options_digest,
    is_fno_symbol,
    load_fno_symbols,
    options_confirmation_note,
)


def _mock_opt_payload() -> dict:
    call_df = pd.DataFrame({"strike": [100.0, 105.0, 110.0], "oi": [1000.0, 5000.0, 2000.0]})
    put_df = pd.DataFrame({"strike": [95.0, 100.0, 105.0], "oi": [800.0, 6000.0, 1500.0]})
    return {
        "underlying": "RELIANCE",
        "call_oi": 8000.0,
        "put_oi": 8300.0,
        "chain_df": pd.concat([call_df, put_df], ignore_index=True),
        "call_chain_df": call_df,
        "put_chain_df": put_df,
        "expiry_date": "2026-06-24T06:00:00.000Z",
        "fallback_used": False,
    }


def test_build_options_audit_fields_computes_walls_and_pcr():
    fields = build_options_audit_fields(_mock_opt_payload(), spot=108.0)
    assert fields["option_chain_unavailable"] is False
    assert fields["call_oi_wall_strike"] == 105.0
    assert fields["put_oi_wall_strike"] == 100.0
    assert math.isclose(fields["pcr"], 8300.0 / 8000.0, rel_tol=1e-6)
    assert not math.isnan(fields["spot_vs_call_wall_pct"])
    assert fields["options_expiry"] == "2026-06-24T06:00:00.000Z"


def test_build_options_audit_fields_unavailable():
    fields = build_options_audit_fields({"option_chain_unavailable": True}, spot=100.0)
    assert fields["option_chain_unavailable"] is True
    assert math.isnan(fields["pcr"])


def test_build_sector_options_digest():
    digest = build_sector_options_digest(_mock_opt_payload(), spot=22000.0, sector_id="defence")
    assert digest["sector"] == "defence"
    assert digest["sector_options_underlying"] == "RELIANCE"
    assert digest["sector_call_wall_strike"] == 105.0
    assert digest["sector_option_chain_unavailable"] is False


def test_is_fno_symbol_uses_allowlist():
    assert is_fno_symbol("RELIANCE")
    assert is_fno_symbol("HAL")
    assert is_fno_symbol("BEL")
    assert not is_fno_symbol("DYNAMATECH")
    assert "RELIANCE" in load_fno_symbols()
    assert "HAL" in load_fno_symbols()


def test_defence_allowlist_overlaps_fno_symbols():
    import json
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    defence = json.loads((repo / "data" / "sector_allowlists" / "defence.json").read_text())[
        "symbols"
    ]
    fno = load_fno_symbols()
    overlap = sorted(sym for sym in defence if sym in fno)
    assert "HAL" in overlap
    assert "BEL" in overlap
    assert "MAZDOCK" in overlap
    assert "DYNAMATECH" not in overlap
    assert len(overlap) >= 10


def test_options_confirmation_note_near_call_wall():
    audit = {
        "option_chain_unavailable": False,
        "close_last": 104.8,
        "call_oi_wall_strike": 105.0,
        "put_oi_wall_strike": 95.0,
        "sell_signal": "trim",
        "cmf_20": -0.1,
        "return_1d_pct": -1.2,
    }
    note = options_confirmation_note(audit)
    assert note is not None
    assert "call OI wall" in note


def test_options_confirmation_note_below_put_support():
    audit = {
        "option_chain_unavailable": False,
        "close_last": 94.0,
        "call_oi_wall_strike": 110.0,
        "put_oi_wall_strike": 95.0,
        "sell_signal": "hold",
        "cmf_20": -0.2,
        "return_1d_pct": -2.0,
    }
    note = options_confirmation_note(audit)
    assert note is not None
    assert "put OI support" in note

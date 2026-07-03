"""Walk-forward fusion arm comparison tests."""

from __future__ import annotations

from sector_walkforward_backtest import (
    FUSION_COMPARISON_ARMS,
    _fusion_arm_kwargs,
    _replay_fusion_on_audit,
    run_fusion_arm_comparison,
    run_walkforward,
)


def _synthetic_rows(sector: str = "pharma_healthcare") -> list[dict]:
    def _row(symbol: str, trade_date: str, ret: float, *, nw: float = 70.0) -> dict:
        return {
            "symbol": symbol,
            "exchange": "NSE",
            "sector": sector,
            "trade_date": trade_date,
            "return_1d_pct": ret,
            "next_week_score": nw,
            "effective_intent_score": 65.0,
            "z_score": 1.0,
            "ema_200_distance_pct": 5.0,
            "atr_14_pct": 2.0,
            "tape_extras": {"cmf_20": 0.05, "return_5d_pct": 2.0},
        }

    rows: list[dict] = []
    for d, r in zip(
        ["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05", "2026-06-08"],
        [1.0, 1.0, -0.5, 2.0, 1.0],
    ):
        rows.append(_row("SYM1", d, r))
    for d, r in zip(
        ["2026-06-02", "2026-06-03", "2026-06-04", "2026-06-20", "2026-06-21"],
        [0.5, 0.5, 0.5, 1.0, 1.0],
    ):
        rows.append(_row("SYM2", d, r, nw=72.0))
    return rows


def test_fusion_arm_kwargs_env_toggle():
    off = _fusion_arm_kwargs("fusion_off")
    on = _fusion_arm_kwargs("fusion_on")
    assert off["env_override"]["TITAN_FUSION_ENABLED"] == "0"
    assert on["env_override"]["TITAN_FUSION_ENABLED"] == "1"


def test_replay_fusion_stamps_audit():
    audit = {
        "effective_intent_score": 70.0,
        "cmf_20": 0.08,
        "sector": "pharma_healthcare",
    }
    _replay_fusion_on_audit(audit, env_override={"TITAN_FUSION_ENABLED": "1"})
    assert "titan_fusion" in audit
    assert audit.get("titan_score") is not None


def test_replay_fusion_off_clears_stamp():
    audit = {"effective_intent_score": 70.0, "titan_score": 65.0, "titan_fusion": {"titan_score": 65.0}}
    _replay_fusion_on_audit(audit, env_override={"TITAN_FUSION_ENABLED": "0"})
    assert "titan_fusion" not in audit
    assert "titan_score" not in audit


def test_run_fusion_arm_comparison_fixture():
    rows = _synthetic_rows("pharma_healthcare")
    report = run_fusion_arm_comparison(
        sector_key="pharma_healthcare",
        start="2026-06-02",
        end="2026-06-04",
        horizons=(1, 5),
        rows=rows,
    )
    assert set(report["arms"]) == set(FUSION_COMPARISON_ARMS)
    for arm in FUSION_COMPARISON_ARMS:
        assert report["arms"][arm]["cohort"]["observations"] >= 0


def test_walkforward_fusion_on_differs_env_from_off():
    shared = dict(
        sector_key="pharma_healthcare",
        start="2026-06-02",
        end="2026-06-04",
        horizons=(1,),
        rows=_synthetic_rows("pharma_healthcare"),
        symbols=["SYM1", "SYM2"],
    )
    off = run_walkforward(**shared, **_fusion_arm_kwargs("fusion_off"))
    on = run_walkforward(**shared, **_fusion_arm_kwargs("fusion_on"))
    assert off["params"]["env_override"]["TITAN_FUSION_ENABLED"] == "0"
    assert on["params"]["env_override"]["TITAN_FUSION_ENABLED"] == "1"

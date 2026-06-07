"""Precious metals allocator: DXY × GSR-band matrix with SGE physical-demand overlay."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from titan_engine import calculate_z_score

# Input column aliases (case-insensitive lookup in generate_features).
_GOLD_KEYS = ("GOLD", "gold", "close_gold")
_SILVER_KEYS = ("SILVER", "silver", "close_silver")
_DXY_KEYS = ("DXY", "dxy", "close_dxy")
_SGE_GOLD_KEYS = ("SGE_GOLD", "sge_gold")
_SGE_SILVER_KEYS = ("SGE_SILVER", "sge_silver")
_SGE_PREMIUM_KEYS = ("SGE_PREMIUM_PCT", "sge_premium_pct", "SGE_PREMIUM")
_SGE_WITHDRAWAL_KEYS = ("SGE_WITHDRAWAL", "sge_withdrawal", "SGE_WITHDRAWALS")

GSR_BAND_LOW = 50.0
GSR_BAND_HIGH = 60.0

_DXY_METALS_EXPOSURE: dict[str, float] = {
    "WEAK": 0.85,
    "STRONG": 0.20,
    "NEUTRAL": 0.40,
}

# Gold / silver share of the metals slice (must sum to 1.0).
_WITHIN_METALS_TILT: dict[str, dict[str, tuple[float, float]]] = {
    "WEAK": {
        "below": (0.70, 0.30),
        "in": (0.50, 0.50),
        "above": (0.20, 0.80),
    },
    "STRONG": {
        "below": (0.60, 0.40),
        "in": (0.55, 0.45),
        "above": (0.40, 0.60),
    },
    "NEUTRAL": {
        "below": (0.55, 0.45),
        "in": (0.50, 0.50),
        "above": (0.35, 0.65),
    },
}

_SGE_MULTIPLIER: dict[str, float] = {
    "TIGHT": 1.15,
    "WEAK": 0.75,
    "NEUTRAL": 1.0,
}

_STRONG_TIGHT_GOLD_FLOOR = 0.20
_STRONG_TIGHT_SILVER_FLOOR = 0.10

_DEFAULT_PM_MACRO_CSV = Path("data/cache/pm_macro_series.csv")
_DEFAULT_BOOK_INR = 10_000_000  # ₹100L


def _safe_float(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v if not math.isnan(v) else float("nan")


def _resolve_series(data: dict[str, pd.Series], keys: tuple[str, ...]) -> pd.Series | None:
    for key in keys:
        if key in data:
            return pd.to_numeric(data[key], errors="coerce").dropna()
        for raw_key, series in data.items():
            if str(raw_key).lower() == key.lower():
                return pd.to_numeric(series, errors="coerce").dropna()
    return None


def _normalize_weights(gold: float, silver: float, cash: float) -> tuple[float, float, float]:
    gold = max(0.0, gold)
    silver = max(0.0, silver)
    cash = max(0.0, cash)
    total = gold + silver + cash
    if total <= 0.0:
        return 0.0, 0.0, 1.0
    return gold / total, silver / total, cash / total


def classify_dxy(dxy_z: float, threshold: float = 1.0) -> str:
    if math.isnan(dxy_z):
        return "NEUTRAL"
    if dxy_z <= -threshold:
        return "WEAK"
    if dxy_z >= threshold:
        return "STRONG"
    return "NEUTRAL"


def classify_gsr_band(gsr_last: float) -> str:
    if math.isnan(gsr_last):
        return "in"
    if gsr_last < GSR_BAND_LOW:
        return "below"
    if gsr_last <= GSR_BAND_HIGH:
        return "in"
    return "above"


def classify_sge_state(
    sge_premium_z: float,
    sge_withdrawal_z: float,
    threshold: float = 1.0,
) -> str:
    tight = (
        not math.isnan(sge_premium_z) and sge_premium_z >= threshold
    ) or (
        not math.isnan(sge_withdrawal_z) and sge_withdrawal_z >= threshold
    )
    weak = not math.isnan(sge_premium_z) and sge_premium_z <= -threshold
    if tight:
        return "TIGHT"
    if weak:
        return "WEAK"
    return "NEUTRAL"


def _gsr_band_label(gsr_band: str) -> str:
    return {
        "below": f"below band (< {GSR_BAND_LOW:.0f})",
        "in": f"in band ({GSR_BAND_LOW:.0f}–{GSR_BAND_HIGH:.0f})",
        "above": f"ABOVE band → Silver catch-up expected",
    }.get(gsr_band, "in band")


def _dxy_meaning(dxy_state: str) -> str:
    return {
        "WEAK": "Tailwind for gold & silver",
        "STRONG": "Headwind for metals — raise cash",
        "NEUTRAL": "No strong dollar signal",
    }.get(dxy_state, "No strong dollar signal")


def _sge_meaning(sge_state: str) -> str:
    return {
        "TIGHT": "China physical demand confirms the move",
        "WEAK": "Physical demand soft — trim conviction",
        "NEUTRAL": "Physical demand neutral",
    }.get(sge_state, "Physical demand neutral")


def _withdrawal_label(withdrawal_z: float, threshold: float = 1.0) -> str:
    if math.isnan(withdrawal_z):
        return "unavailable"
    if withdrawal_z >= threshold:
        return "elevated"
    if withdrawal_z <= -threshold:
        return "light"
    return "normal"


def _compute_conviction(dxy_state: str, gsr_band: str, sge_state: str) -> str:
    silver_catch_up = dxy_state == "WEAK" and gsr_band == "above" and sge_state == "TIGHT"
    gold_defensive = dxy_state == "STRONG" and gsr_band == "below"
    if silver_catch_up or (dxy_state == "WEAK" and gsr_band == "below" and sge_state == "TIGHT"):
        return "HIGH"
    if dxy_state == "STRONG" and sge_state == "TIGHT":
        return "MIXED"
    if dxy_state == "STRONG" and sge_state == "WEAK":
        return "LOW"
    if dxy_state == "NEUTRAL" and gsr_band == "in" and sge_state == "NEUTRAL":
        return "LOW"
    if (
        (dxy_state == "WEAK" and sge_state in ("TIGHT", "NEUTRAL"))
        or (gsr_band == "above" and sge_state == "TIGHT")
        or (dxy_state == "NEUTRAL" and gsr_band != "in")
    ):
        return "MODERATE"
    return "MODERATE"


def _build_read_line(dxy_state: str, gsr_band: str, sge_state: str) -> str:
    parts: list[str] = []
    if dxy_state == "WEAK":
        parts.append("Favourable for metals")
    elif dxy_state == "STRONG":
        parts.append("Caution on metals")
    else:
        parts.append("Neutral macro for metals")

    if gsr_band == "above":
        parts.append("Silver leading")
    elif gsr_band == "below":
        parts.append("Gold favoured on ratio")
    else:
        parts.append("Balanced GSR")

    if sge_state == "TIGHT":
        parts.append("Physical demand confirms")
    elif sge_state == "WEAK":
        parts.append("Physical demand weak")
    else:
        parts.append("Physical demand neutral")

    return " · ".join(parts)


def _build_takeaway(dxy_state: str, gsr_band: str, sge_state: str, conviction: str) -> str:
    if dxy_state == "WEAK" and gsr_band == "above" and sge_state == "TIGHT":
        return (
            "Silver catch-up trade with physical confirmation — "
            "size up silver, keep gold as hedge."
        )
    if dxy_state == "STRONG" and sge_state == "TIGHT":
        return (
            "Conflicting signals — dollar strong but Shanghai tight; "
            "keep minimum metals floors, stay defensive."
        )
    if dxy_state == "STRONG":
        return "Strong dollar regime — prioritize cash, keep metals as hedge only."
    if gsr_band == "below" and dxy_state in ("WEAK", "NEUTRAL"):
        return "Gold favoured on low GSR — tilt metals toward gold, watch silver lag."
    if gsr_band == "above" and sge_state != "WEAK":
        return "Elevated GSR favours silver catch-up — overweight silver within metals."
    if conviction == "LOW":
        return "No clear edge — hold balanced allocation and wait for macro alignment."
    return "Partial alignment — maintain balanced metals tilt and monitor DXY/GSR/SGE."


def _build_waterfall_steps(
    dxy_state: str,
    gsr_band: str,
    sge_state: str,
    total_metals_pct: float,
    gold_share: float,
    silver_share: float,
    sge_multiplier: float,
) -> list[str]:
    steps: list[str] = [
        f"1. DXY {dxy_state.lower()} → deploy {total_metals_pct:.0f}% to metals",
    ]
    if gsr_band == "above":
        steps.append(
            f"2. GSR > {GSR_BAND_HIGH:.0f} → tilt "
            f"{silver_share * 100:.0f}% silver / {gold_share * 100:.0f}% gold within metals"
        )
    elif gsr_band == "below":
        steps.append(
            f"2. GSR < {GSR_BAND_LOW:.0f} → tilt "
            f"{gold_share * 100:.0f}% gold / {silver_share * 100:.0f}% silver within metals"
        )
    else:
        steps.append(
            f"2. GSR in band → balanced "
            f"{gold_share * 100:.0f}% gold / {silver_share * 100:.0f}% silver within metals"
        )

    if sge_state == "TIGHT":
        steps.append(
            f"3. SGE tight → ×{sge_multiplier:.2f} metals exposure (+15% conviction boost)"
        )
    elif sge_state == "WEAK":
        steps.append(f"3. SGE weak → ×{sge_multiplier:.2f} metals exposure (trim)")
    else:
        steps.append("3. SGE neutral → no physical-demand adjustment")
    return steps


def load_pm_macro_series_from_csv(
    path: str | Path | None = None,
) -> dict[str, pd.Series] | None:
    """
    Load PM macro series from CSV cache.

    Expected columns: date, GOLD, SILVER, DXY, SGE_PREMIUM_PCT, SGE_WITHDRAWAL.
    Returns None when the file is missing or empty.
    """
    csv_path = Path(path or os.environ.get("TITAN_PM_MACRO_CSV", _DEFAULT_PM_MACRO_CSV))
    if not csv_path.is_file():
        return None
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None
    if df.empty:
        return None
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date")
    out: dict[str, pd.Series] = {}
    for col in df.columns:
        if str(col).lower() == "date":
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if not series.empty:
            out[str(col).upper()] = series.reset_index(drop=True)
    required = ("GOLD", "SILVER", "DXY")
    if not all(k in out for k in required):
        return None
    return out


def generate_synthetic_pm_macro_series(n: int = 35, seed: int = 42) -> dict[str, pd.Series]:
    """Deterministic synthetic series for tests and demos (weak DXY, high GSR, tight SGE)."""
    rng = pd.Series(range(n))
    dxy = pd.Series([105.0 - i * 0.35 for i in range(n)])
    gold = pd.Series([2000.0 + i * 0.5 for i in range(n)])
    silver = pd.Series([25.0 + i * 0.02 for i in range(n)])
    premium = pd.Series([1.0 + (i / max(n - 1, 1)) * 1.5 for i in range(n)])
    withdrawal = pd.Series([100.0 + i * 2.5 for i in range(n)])
    _ = seed  # reserved for future stochastic fixtures
    return {
        "GOLD": gold,
        "SILVER": silver,
        "DXY": dxy,
        "SGE_PREMIUM_PCT": premium,
        "SGE_WITHDRAWAL": withdrawal,
        "DATE": rng,
    }


_UNSET_BOOK = object()


def resolve_pm_book_value_inr(book_value_inr: float | None = None) -> float | None:
    """Return book value in INR for allocation dollar lines, or None to omit."""
    if book_value_inr is not None:
        return float(book_value_inr)
    raw = os.environ.get("TITAN_PM_BOOK_INR", str(_DEFAULT_BOOK_INR)).strip()
    if not raw or raw.lower() in ("0", "none", "off", "false"):
        return None
    try:
        return float(raw)
    except ValueError:
        return float(_DEFAULT_BOOK_INR)


def format_precious_metals_digest_lines(
    result: dict[str, Any],
    features: dict[str, Any],
    as_of_date: str,
    book_value_inr: float | None = _UNSET_BOOK,
) -> list[str]:
    """Render the approved precious-metals macro email section."""
    dxy_state = str(result.get("dxy_state", "NEUTRAL"))
    gsr_band = str(result.get("gsr_band", "in"))
    sge_state = str(result.get("sge_state", "NEUTRAL"))
    conviction = str(result.get("conviction", "MODERATE"))

    dxy_z = _safe_float(result.get("dxy_z"))
    gsr_last = _safe_float(features.get("gsr_last", result.get("gsr_last")))
    premium_pct = _safe_float(features.get("sge_premium_pct"))
    premium_z = _safe_float(result.get("sge_premium_z"))
    withdrawal_z = _safe_float(result.get("sge_withdrawal_z"))

    gold_pct = _safe_float(result.get("gold_pct"))
    silver_pct = _safe_float(result.get("silver_pct"))
    cash_pct = _safe_float(result.get("cash_pct"))

    if book_value_inr is _UNSET_BOOK:
        book_inr = resolve_pm_book_value_inr()
    else:
        book_inr = book_value_inr
    book_lakhs = (book_inr / 100_000.0) if book_inr else None
    book_usd = (book_inr / 100.0) if book_inr else None

    def _alloc_line(label: str, pct: float) -> str:
        base = f"  {label:<6} {pct:5.1f}%"
        if book_usd is not None and book_lakhs is not None:
            usd = pct / 100.0 * book_usd
            if label.strip().lower().startswith("gold"):
                return f"{base}  (${usd:,.0f} on ₹{book_lakhs:.0f}L book)"
            return f"{base}  (${usd:,.0f})"
        return base

    premium_sign = "+" if premium_pct >= 0 else ""
    withdrawal_desc = _withdrawal_label(withdrawal_z)

    lines = [
        "--- Precious metals macro ---",
        f"As of: {as_of_date} (EOD)",
        f"Read: {result.get('read_line', _build_read_line(dxy_state, gsr_band, sge_state))}",
        "",
        "▸ Macro backdrop (DXY)",
        f"  Dollar: {dxy_state} (Z = {dxy_z:+.2f})",
        f"  Meaning: {_dxy_meaning(dxy_state)}",
        "",
        "▸ Relative value (GSR)",
        f"  Gold/Silver ratio: {gsr_last:.1f} (band: {GSR_BAND_LOW:.0f}–{GSR_BAND_HIGH:.0f})",
        f"  Zone: {_gsr_band_label(gsr_band)}",
    ]
    if gsr_band == "above":
        lines.append("  Silver should outpace gold in this phase")
    elif gsr_band == "below":
        lines.append("  Gold should outpace silver in this phase")
    else:
        lines.append("  Gold and silver expected to move in line")

    lines.extend(
        [
            "",
            "▸ Physical demand (SGE)",
            (
                f"  Shanghai premium: {premium_sign}{premium_pct:.1f}% (Z = {premium_z:+.2f}) · "
                f"{sge_state}"
            ),
            f"  Withdrawals: {withdrawal_desc} (Z = {withdrawal_z:+.2f})",
            f"  Meaning: {_sge_meaning(sge_state)}",
            "",
            "▸ Recommended allocation",
            _alloc_line("Gold:", gold_pct),
            _alloc_line("Silver:", silver_pct),
            _alloc_line("Cash:", cash_pct),
            f"  Conviction: {conviction} (macro + GSR + SGE aligned)",
            "",
            "▸ How we got here",
        ]
    )
    for step in result.get("waterfall_steps") or []:
        lines.append(f"  {step}")
    lines.extend(
        [
            "",
            "▸ One-line takeaway",
            f"  {result.get('takeaway', _build_takeaway(dxy_state, gsr_band, sge_state, conviction))}",
        ]
    )
    return lines


class PreciousMetalsAlgo:
    """DXY × GSR-band matrix allocator with SGE physical-demand overlay."""

    def __init__(
        self,
        z_window: int = 252,
        z_threshold: float = 1.0,
        sge_z_threshold: float = 1.0,
    ) -> None:
        if z_window < 2:
            raise ValueError("z_window must be >= 2")
        if z_threshold <= 0.0:
            raise ValueError("z_threshold must be > 0")
        if sge_z_threshold <= 0.0:
            raise ValueError("sge_z_threshold must be > 0")
        self.z_window = z_window
        self.z_threshold = z_threshold
        self.sge_z_threshold = sge_z_threshold

    def generate_features(self, data: dict[str, pd.Series]) -> dict[str, Any]:
        """
        Build macro + SGE features from price series.

        Expected keys (any alias accepted): GOLD, SILVER, DXY.
        Optional SGE: SGE_GOLD (+ GOLD for premium), SGE_PREMIUM_PCT, SGE_WITHDRAWAL.
        """
        if not data:
            raise ValueError("data must contain at least one price series")

        gold = _resolve_series(data, _GOLD_KEYS)
        silver = _resolve_series(data, _SILVER_KEYS)
        dxy = _resolve_series(data, _DXY_KEYS)
        sge_gold = _resolve_series(data, _SGE_GOLD_KEYS)
        sge_premium = _resolve_series(data, _SGE_PREMIUM_KEYS)
        sge_withdrawal = _resolve_series(data, _SGE_WITHDRAWAL_KEYS)

        if gold is None or gold.empty:
            raise ValueError("GOLD series is required")
        if silver is None or silver.empty:
            raise ValueError("SILVER series is required")
        if dxy is None or dxy.empty:
            raise ValueError("DXY series is required")

        aligned = pd.DataFrame({"gold": gold, "silver": silver, "dxy": dxy}).dropna()
        if aligned.empty:
            raise ValueError("GOLD, SILVER, and DXY have no overlapping observations")

        gsr = aligned["gold"] / aligned["silver"].replace(0.0, float("nan"))
        gsr = gsr.dropna()
        if gsr.empty:
            raise ValueError("GSR series is empty after alignment")

        dxy_z = calculate_z_score(aligned["dxy"], window=self.z_window)
        gsr_z = calculate_z_score(gsr, window=self.z_window)

        premium_pct = float("nan")
        if sge_premium is not None and not sge_premium.empty:
            premium_pct = float(sge_premium.iloc[-1])
        elif sge_gold is not None and not sge_gold.empty:
            sge_last = float(sge_gold.iloc[-1])
            gold_last = float(aligned["gold"].iloc[-1])
            if gold_last > 0.0:
                premium_pct = ((sge_last / gold_last) - 1.0) * 100.0

        sge_premium_z = float("nan")
        if not math.isnan(premium_pct):
            premium_series = sge_premium.copy() if sge_premium is not None else None
            if premium_series is None and sge_gold is not None:
                merged = pd.DataFrame({"sge": sge_gold, "gold": gold}).dropna()
                if not merged.empty:
                    premium_series = (merged["sge"] / merged["gold"] - 1.0) * 100.0
            if premium_series is not None and not premium_series.empty:
                sge_premium_z = calculate_z_score(premium_series, window=self.z_window)

        sge_withdrawal_z = float("nan")
        if sge_withdrawal is not None and not sge_withdrawal.empty:
            sge_withdrawal_z = calculate_z_score(sge_withdrawal, window=self.z_window)

        return {
            "dxy_z": dxy_z,
            "gsr_z": gsr_z,
            "gsr_last": float(gsr.iloc[-1]),
            "sge_premium_pct": premium_pct,
            "sge_premium_z": sge_premium_z,
            "sge_withdrawal_z": sge_withdrawal_z,
            "observations": len(aligned),
        }

    def execute_allocation_logic(self, features: dict[str, Any]) -> dict[str, Any]:
        """
        DXY sets total metals exposure; GSR band sets within-metals tilt; SGE confirms.

        Returns allocation weights plus rich metadata for the email formatter.
        """
        if not features:
            raise ValueError("features dict is required")

        dxy_z = _safe_float(features.get("dxy_z"))
        gsr_last = _safe_float(features.get("gsr_last"))
        sge_premium_z = _safe_float(features.get("sge_premium_z"))
        sge_withdrawal_z = _safe_float(features.get("sge_withdrawal_z"))

        dxy_state = classify_dxy(dxy_z, self.z_threshold)
        gsr_band = classify_gsr_band(gsr_last)
        sge_state = classify_sge_state(
            sge_premium_z, sge_withdrawal_z, self.sge_z_threshold
        )

        total_metals = _DXY_METALS_EXPOSURE[dxy_state]
        gold_share, silver_share = _WITHIN_METALS_TILT[dxy_state][gsr_band]

        gold = total_metals * gold_share
        silver = total_metals * silver_share
        cash = 1.0 - total_metals
        base_g, base_s, base_c = gold, silver, cash

        sge_multiplier = _SGE_MULTIPLIER[sge_state]
        target_metals = min(1.0, total_metals * sge_multiplier)
        delta = target_metals - total_metals

        if delta > 0.0:
            shift = min(delta, cash)
            gold += shift * gold_share
            silver += shift * silver_share
            cash -= shift
        elif delta < 0.0:
            release = -delta
            gold -= release * gold_share
            silver -= release * silver_share
            cash += release

        sge_adjustments: list[str] = []
        if sge_state == "TIGHT":
            sge_adjustments.append("sge_tightness")
        elif sge_state == "WEAK":
            sge_adjustments.append("sge_weak_demand")

        if dxy_state == "STRONG" and sge_state == "TIGHT":
            if gold < _STRONG_TIGHT_GOLD_FLOOR:
                need = _STRONG_TIGHT_GOLD_FLOOR - gold
                gold = _STRONG_TIGHT_GOLD_FLOOR
                cash = max(0.0, cash - need)
                sge_adjustments.append("sge_bear_floor")
            if silver < _STRONG_TIGHT_SILVER_FLOOR:
                need = _STRONG_TIGHT_SILVER_FLOOR - silver
                silver = _STRONG_TIGHT_SILVER_FLOOR
                cash = max(0.0, cash - need)
                if "sge_bear_floor" not in sge_adjustments:
                    sge_adjustments.append("sge_bear_floor")

        gold, silver, cash = _normalize_weights(gold, silver, cash)
        conviction = _compute_conviction(dxy_state, gsr_band, sge_state)
        read_line = _build_read_line(dxy_state, gsr_band, sge_state)
        takeaway = _build_takeaway(dxy_state, gsr_band, sge_state, conviction)
        waterfall_steps = _build_waterfall_steps(
            dxy_state,
            gsr_band,
            sge_state,
            total_metals * 100.0,
            gold_share,
            silver_share,
            sge_multiplier,
        )

        waterfall = [
            f"dxy:{dxy_state}",
            f"gsr_band:{gsr_band}",
            f"sge:{sge_state}",
            f"base=({base_g:.2f},{base_s:.2f},{base_c:.2f})",
            f"sge_mult={sge_multiplier:.2f}",
            f"final=({gold:.2f},{silver:.2f},{cash:.2f})",
        ]

        return {
            "dxy_state": dxy_state,
            "gsr_band": gsr_band,
            "sge_state": sge_state,
            "conviction": conviction,
            "read_line": read_line,
            "takeaway": takeaway,
            "waterfall_steps": waterfall_steps,
            "gold_pct": round(gold * 100.0, 2),
            "silver_pct": round(silver * 100.0, 2),
            "cash_pct": round(cash * 100.0, 2),
            "base_gold_pct": round(base_g * 100.0, 2),
            "base_silver_pct": round(base_s * 100.0, 2),
            "base_cash_pct": round(base_c * 100.0, 2),
            "total_metals_pct": round(total_metals * 100.0, 2),
            "sge_multiplier": sge_multiplier,
            "dxy_z": dxy_z,
            "gsr_z": _safe_float(features.get("gsr_z")),
            "gsr_last": gsr_last,
            "sge_premium_z": sge_premium_z,
            "sge_withdrawal_z": sge_withdrawal_z,
            "sge_adjustments": sge_adjustments,
            "waterfall": waterfall,
        }

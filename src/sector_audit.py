"""Sector-wide equity audits: CSV universe, parallel Breeze fetches, Supabase + digest email."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config_loader import TitanConfig, load_config
from sector_registry import SectorInstrument, load_sector_instruments

logger = logging.getLogger(__name__)

# Parallel sector fetches (each worker opens its own Breeze session; keep modest for API limits).
MAX_WORKERS = 4


def build_equity_live_audit(
    cfg: TitanConfig,
    breeze: Any,
    inst: SectorInstrument,
    *,
    sector_id: str,
    lookback_calendar_days: int = 60,
) -> tuple[dict[str, Any], str]:
    """
    Cash-market metrics only (z-score, volume absorption, intent blend).
    Option chain / PCR are skipped for sector equities (expiry rules differ per name); flagged in audit.
    """
    import pandas as pd

    from brain import generate_titan_narrative
    from breeze_client import fetch_equity_data, volume_absorption_ratio
    from titan_engine import calculate_intent_score, calculate_z_score

    df = fetch_equity_data(
        cfg,
        inst.symbol,
        inst.exchange,
        breeze=breeze,
        lookback_calendar_days=lookback_calendar_days,
    )
    if df.empty:
        raise RuntimeError(
            f"[Breeze] No rows returned for {inst.symbol} ({inst.exchange}); task BLOCKED"
        )
    close_col = "close" if "close" in df.columns else df.columns[-1]
    series = pd.to_numeric(df[close_col], errors="coerce")
    z = calculate_z_score(series, window=20)
    absorption = volume_absorption_ratio(df)
    pcr = float("nan")
    intent = calculate_intent_score(pcr, z, absorption)
    audit: dict[str, Any] = {
        "benchmark": "equity",
        "sector_mode": True,
        "sector": sector_id,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "z_score": z,
        "absorption_ratio": absorption,
        "pcr": pcr,
        "put_oi": 0.0,
        "call_oi": 0.0,
        "oi_wall": {"strike": float("nan"), "oi": float("nan")},
        "option_expiry": None,
        "intent_score": intent,
        "rows": len(df),
        "option_chain_unavailable": True,
    }
    post = generate_titan_narrative(audit, api_key=cfg.gemini_api_key)
    return audit, post


def _process_one(cfg: TitanConfig, sector_id: str, inst: SectorInstrument) -> dict[str, Any]:
    from breeze_client import create_breeze_session
    from supabase_log import save_audit_log

    breeze = create_breeze_session(cfg)
    audit, post = build_equity_live_audit(cfg, breeze, inst, sector_id=sector_id)
    save_audit_log({"audit": audit, "post": post}, cfg)
    return {
        "ok": True,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "post": post,
        "error": None,
    }


def run_sector_live(sector_id: str, *, max_workers: int | None = None) -> None:
    from email_notify import send_success_post_email

    cfg = load_config()
    instruments = load_sector_instruments(sector_id)
    if not instruments:
        raise RuntimeError(f"[Sector] No instruments loaded for sector {sector_id!r}")

    workers = max_workers if max_workers is not None else MAX_WORKERS
    workers = max(1, min(int(workers), 16))

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_process_one, cfg, sector_id, inst): inst for inst in instruments
        }
        for fut in as_completed(future_map):
            inst = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                logger.exception("Sector instrument failed: %s %s", inst.symbol, inst.exchange)
                results.append(
                    {
                        "ok": False,
                        "symbol": inst.symbol,
                        "exchange": inst.exchange,
                        "post": "",
                        "error": str(e),
                    }
                )

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        raise RuntimeError(
            f"[Sector] All {len(results)} instruments failed for sector {sector_id!r}"
        )

    lines = [f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded\n"]
    for r in sorted(results, key=lambda x: (x["symbol"], x["exchange"])):
        if r.get("ok"):
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) ---\n")
            lines.append((r.get("post") or "").strip())
        else:
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) FAILED ---\n{r.get('error', '')}\n")

    digest = "\n".join(lines).strip()
    send_success_post_email(digest, subject_prefix=f"Titan V12.0 sector {sector_id}")
    print(digest)

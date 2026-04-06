"""Sector-wide equity audits: CSV universe, parallel Breeze fetches, Supabase + digest email."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config_loader import TitanConfig, load_config
from sector_registry import SectorInstrument, load_sector_instruments

logger = logging.getLogger(__name__)

# Parallel sector fetches (each worker opens its own Breeze session; keep modest for API limits).
MAX_WORKERS = 4

# Serialize Gemini calls so sector threads do not burst past rate limits together.
_GEMINI_SECTOR_LOCK = threading.Lock()


def build_equity_live_audit(
    cfg: TitanConfig,
    breeze: Any,
    inst: SectorInstrument,
    *,
    sector_id: str,
    lookback_calendar_days: int = 60,
    with_narrative: bool = True,
) -> tuple[dict[str, Any], str]:
    """
    Cash-market metrics only (z-score, volume absorption, intent blend).
    Option chain / PCR are skipped for sector equities (expiry rules differ per name); flagged in audit.
    """
    import pandas as pd

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
    if not with_narrative:
        return audit, ""
    from brain import generate_titan_narrative

    with _GEMINI_SECTOR_LOCK:
        post = generate_titan_narrative(audit, api_keys=cfg.gemini_api_keys)
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


def _process_one_metrics(cfg: TitanConfig, sector_id: str, inst: SectorInstrument) -> dict[str, Any]:
    """Breeze + metrics only; no Gemini (used with --sector-digest)."""
    from breeze_client import create_breeze_session

    breeze = create_breeze_session(cfg)
    audit, _ = build_equity_live_audit(
        cfg, breeze, inst, sector_id=sector_id, with_narrative=False
    )
    return {
        "ok": True,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "audit": audit,
        "error": None,
    }


def run_sector_live(
    sector_id: str,
    *,
    max_workers: int | None = None,
    max_symbols: int | None = None,
    digest: bool = True,
) -> None:
    from email_notify import send_success_post_email

    cfg = load_config()
    instruments = load_sector_instruments(sector_id)
    if not instruments:
        raise RuntimeError(f"[Sector] No instruments loaded for sector {sector_id!r}")

    if max_symbols is not None:
        instruments = instruments[: max(0, int(max_symbols))]

    workers = max_workers if max_workers is not None else MAX_WORKERS
    workers = max(1, min(int(workers), 16))

    results: list[dict[str, Any]] = []
    worker = _process_one_metrics if digest else _process_one
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(worker, cfg, sector_id, inst): inst for inst in instruments}
        for fut in as_completed(future_map):
            inst = future_map[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                logger.exception("Sector instrument failed: %s %s", inst.symbol, inst.exchange)
                err_row: dict[str, Any] = {
                    "ok": False,
                    "symbol": inst.symbol,
                    "exchange": inst.exchange,
                    "error": str(e),
                }
                if digest:
                    err_row["audit"] = None
                else:
                    err_row["post"] = ""
                results.append(err_row)

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        raise RuntimeError(
            f"[Sector] All {len(results)} instruments failed for sector {sector_id!r}"
        )

    if digest:
        from brain import generate_sector_digest_narrative
        from supabase_log import save_audit_log

        ok_results = [r for r in results if r.get("ok")]
        audits = [r["audit"] for r in ok_results]
        with _GEMINI_SECTOR_LOCK:
            post = generate_sector_digest_narrative(
                audits, sector_id=sector_id, api_keys=cfg.gemini_api_keys
            )
        for r in ok_results:
            save_audit_log({"audit": r["audit"], "post": post}, cfg)

        lines = [
            f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded "
            f"(digest mode: 1 Gemini call)\n",
            "",
            post.strip(),
            "",
            "--- Per-symbol metrics ---",
        ]
        for r in sorted(ok_results, key=lambda x: (x["symbol"], x["exchange"])):
            a = r["audit"]
            lines.append(
                f"{r['symbol']} ({r['exchange']}): z={a.get('z_score')} "
                f"intent={a.get('intent_score')} absorption={a.get('absorption_ratio')} "
                f"rows={a.get('rows')}"
            )
        for r in sorted(
            (x for x in results if not x.get("ok")),
            key=lambda x: (x["symbol"], x["exchange"]),
        ):
            lines.append("")
            lines.append(f"--- {r['symbol']} ({r['exchange']}) FAILED ---")
            lines.append(r.get("error", "") or "")
        digest_text = "\n".join(lines).strip()
        send_success_post_email(digest_text, subject_prefix=f"Titan V12.0 sector {sector_id}")
        print(digest_text)
        return

    lines = [f"Titan sector run: {sector_id!r} — {ok_count}/{len(results)} succeeded\n"]
    for r in sorted(results, key=lambda x: (x["symbol"], x["exchange"])):
        if r.get("ok"):
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) ---\n")
            lines.append((r.get("post") or "").strip())
        else:
            lines.append(f"\n--- {r['symbol']} ({r['exchange']}) FAILED ---\n{r.get('error', '')}\n")

    digest_out = "\n".join(lines).strip()
    send_success_post_email(digest_out, subject_prefix=f"Titan V12.0 sector {sector_id}")
    print(digest_out)

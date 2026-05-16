"""Titan V12.0 controller: wire engine, optional Breeze, brain, Supabase."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent
# override=True: if OS has GEMINI_API_KEY="" it would otherwise block .env.
load_dotenv(ROOT / ".env", override=True)


def _gemini_api_keys_for_dry_run() -> tuple[str, ...]:
    """Keys from env after load_dotenv; fall back to parsing .env file (unsaved-editor edge cases)."""
    keys = try_parse_gemini_api_keys_from_env()
    if keys:
        return keys
    env_path = ROOT / ".env"
    if env_path.is_file():
        vals = dotenv_values(env_path)
        merged = dict(os.environ)
        for k, v in vals.items():
            if v is not None:
                merged[k] = str(v)
        return try_parse_gemini_api_keys_from_env(merged)
    return ()

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from brain import generate_titan_narrative
from compliance import compliance_scan
from config_loader import load_config, try_parse_gemini_api_keys_from_env
from email_notify import send_failure_email, send_success_post_email
from protocol_runtime import available_clusters, resolve_protocol_runs
from titan_engine import (
    calculate_absorption_ratio,
    calculate_intent_score,
    calculate_z_score,
    find_oi_walls,
    get_pcr,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("titan.main")


def build_dummy_audit() -> dict:
    """Full simulation with dummy OHLC and option-chain style inputs."""
    import pandas as pd

    prices = [100.0] * 25
    prices[-1] = 108.0
    ohlc = pd.DataFrame({"close": prices})
    z = calculate_z_score(ohlc["close"], window=20)
    absorption = calculate_absorption_ratio(2_000_000.0, 1_000_000.0)
    pcr = get_pcr(1.2e6, 1.0e6)
    chain = pd.DataFrame(
        {
            "strike": [21000, 21500, 22000, 22500],
            "oi": [1e5, 5e5, 2e5, 8e5],
        }
    )
    wall = find_oi_walls(chain)
    intent = calculate_intent_score(pcr, z, absorption)
    return {
        "z_score": z,
        "absorption_ratio": absorption,
        "pcr": pcr,
        "oi_wall": wall,
        "intent_score": intent,
        "symbol": "NIFTY",
    }


def format_social_post(body: str) -> str:
    """X/LinkedIn friendly block (no extra compliance issues)."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return "\n\n".join(lines)


def _load_macro_snapshot(path_raw: str) -> dict:
    path = Path(path_raw).expanduser()
    if not path.is_file():
        raise ValueError(f"Macro snapshot file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Macro snapshot is not valid JSON: {path}") from e
    if not isinstance(payload, dict):
        raise ValueError("Macro snapshot JSON must be an object")
    return payload


def _load_event_snapshot(path_raw: str) -> dict:
    path = Path(path_raw).expanduser()
    if not path.is_file():
        raise ValueError(f"Event snapshot file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Event snapshot is not valid JSON: {path}") from e
    if not isinstance(payload, dict):
        raise ValueError("Event snapshot JSON must be an object with an 'events' array")
    events = payload.get("events")
    if events is not None and not isinstance(events, list):
        raise ValueError("Event snapshot field 'events' must be a list when provided")
    return payload


def _parse_cluster_list(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    items = tuple(x.strip() for x in raw.split(",") if x.strip())
    allowed = set(available_clusters())
    bad = [x for x in items if x.lower() not in allowed]
    if bad:
        raise ValueError(
            "Unknown protocol clusters: "
            + ", ".join(sorted(set(bad)))
            + f" (allowed: {', '.join(sorted(allowed))})"
        )
    return tuple(x.lower() for x in items)


def _parse_csv_list(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def run_all_sectors(
    *,
    max_workers: int | None,
    max_symbols: int | None,
    digest: bool,
    exclude_sectors: tuple[str, ...],
    macro_snapshot: dict | None,
    event_snapshot: dict | None,
) -> None:
    from sector_audit import run_sector_live
    from sector_registry import list_active_sector_ids

    sectors = [s for s in list_active_sector_ids(include_unknown=False) if s not in set(exclude_sectors)]
    if not sectors:
        raise RuntimeError("No active sectors found after exclusions.")
    logger.info("Running all sectors (%s): %s", len(sectors), ", ".join(sectors))
    failed: list[str] = []
    for sid in sectors:
        logger.info("Running sector: %s", sid)
        try:
            run_kwargs = dict(
                max_workers=max_workers,
                max_symbols=max_symbols,
                digest=digest,
            )
            if macro_snapshot is not None:
                run_kwargs["macro_snapshot"] = macro_snapshot
            if event_snapshot is not None:
                run_kwargs["event_snapshot"] = event_snapshot
            run_sector_live(sid, **run_kwargs)
        except Exception:
            failed.append(sid)
            logger.exception("Sector run failed: %s", sid)
    if failed:
        raise RuntimeError(f"All-sector run completed with failures in: {', '.join(failed)}")


def run_protocol_window(
    *,
    window: str | None,
    clusters: tuple[str, ...],
    strict_window: bool,
    macro_snapshot: dict | None,
    event_snapshot: dict | None,
    max_workers: int | None,
    max_symbols: int | None,
) -> None:
    from sector_audit import run_sector_live

    runs = resolve_protocol_runs(
        window=window,
        clusters=clusters if clusters else None,
        strict_window=strict_window,
    )
    if not runs:
        logger.info("No protocol windows due now (strict-window gating).")
        return
    for run in runs:
        logger.info(
            "Protocol run window=%s cluster=%s symbols=%s",
            run.window,
            run.cluster_id,
            len(run.instruments),
        )
        instruments = list(run.instruments)
        if max_symbols is not None:
            instruments = instruments[: max(0, int(max_symbols))]
        run_sector_live(
            run.sector_id,
            max_workers=max_workers,
            max_symbols=None,
            digest=True,
            macro_snapshot=macro_snapshot,
            event_snapshot=event_snapshot,
            instruments_override=instruments,
        )


def run_dry_run() -> None:
    audit = build_dummy_audit()
    ok, hits = compliance_scan(json.dumps(audit))
    logger.info("dummy audit compliance pre-check (json): ok=%s hits=%s", ok, hits)
    keys = _gemini_api_keys_for_dry_run()
    if not keys:
        logger.warning("No Gemini API keys configured; skipping narrative (dry metrics only).")
        print(json.dumps(audit, indent=2))
        return
    post = generate_titan_narrative(audit, api_keys=keys)
    print(format_social_post(post))


def run_live() -> None:
    cfg = load_config()
    import pandas as pd

    from analysis_store import (
        build_comparison_payload,
        persist_llm_digest_memory,
        persist_sector_run_analytics,
        quality_checks_for_run,
        update_sector_period_rollups,
    )
    from breeze_client import (
        create_breeze_session,
        fetch_nifty_data,
        fetch_nifty_option_metrics_with_expiry_fallback,
        volume_absorption_ratio,
    )
    from supabase_log import save_audit_log

    breeze = create_breeze_session(cfg)
    df = fetch_nifty_data(cfg, breeze=breeze, lookback_calendar_days=60)
    if df.empty:
        raise RuntimeError("[Breeze] No NIFTY rows returned; task BLOCKED")
    close_col = "close" if "close" in df.columns else df.columns[-1]
    series = pd.to_numeric(df[close_col], errors="coerce")
    z = calculate_z_score(series, window=20)
    absorption = volume_absorption_ratio(df)
    opt = fetch_nifty_option_metrics_with_expiry_fallback(breeze)
    pcr = get_pcr(opt["put_oi"], opt["call_oi"])
    oi_wall = find_oi_walls(opt["chain_df"]) if not opt["chain_df"].empty else {"strike": float("nan"), "oi": float("nan")}
    intent = calculate_intent_score(pcr, z, absorption)
    audit = {
        "benchmark": "index",
        "sector_mode": False,
        "sector": "nifty_index",
        "symbol": "NIFTY",
        "exchange": "NSE",
        "z_score": z,
        "absorption_ratio": absorption,
        "pcr": pcr,
        "put_oi": opt["put_oi"],
        "call_oi": opt["call_oi"],
        "oi_wall": oi_wall,
        "option_expiry": opt["expiry_date"],
        "option_chain_fallback_used": bool(opt.get("fallback_used", False)),
        "option_chain_expiry_try_index": opt.get("expiry_try_index"),
        "option_chain_expiry_tries": opt.get("expiry_tries"),
        "intent_score": intent,
        "effective_intent_score": intent,
        "rows": len(df),
    }
    if opt.get("option_chain_unavailable"):
        audit["option_chain_unavailable"] = True
    persist_meta = persist_sector_run_analytics(
        cfg,
        sector="nifty_index",
        audits=[audit],
        mode="index_live",
        ok_count=1,
        total_count=1,
    )
    update_sector_period_rollups(cfg, sector="nifty_index")
    comparison = build_comparison_payload(cfg, sector="nifty_index")
    qc_warnings = quality_checks_for_run([audit], comparison=comparison)
    if comparison.get("enabled"):
        audit["comparison_context"] = comparison
    if qc_warnings:
        audit["quality_warnings"] = qc_warnings

    post = generate_titan_narrative(audit, api_keys=cfg.gemini_api_keys)
    if persist_meta.get("persisted") and persist_meta.get("run_id"):
        persist_llm_digest_memory(
            cfg,
            run_id=str(persist_meta["run_id"]),
            sector="nifty_index",
            prompt_facts=comparison if comparison.get("enabled") else {"enabled": False},
            output_text=post,
            model_name=None,
        )
    save_audit_log({"audit": audit, "post": post}, cfg)
    send_success_post_email(post)
    print(format_social_post(post))


def main() -> None:
    p = argparse.ArgumentParser(description="Titan V12.0")
    p.add_argument("--dry-run", action="store_true", help="Simulate with dummy data")
    p.add_argument("--live", action="store_true", help="Fetch NIFTY via Breeze and persist")
    p.add_argument(
        "--sector",
        type=str,
        default="",
        metavar="ID",
        help="Sector audit: load active symbols from Supabase sector registry (e.g. defence), parallel equity runs",
    )
    p.add_argument(
        "--sector-workers",
        type=int,
        default=None,
        metavar="N",
        help="Thread pool size for --sector (default: sector_audit.MAX_WORKERS)",
    )
    p.add_argument(
        "--sector-max-symbols",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N symbols from the Supabase sector list (free-tier friendly)",
    )
    p.add_argument(
        "--all-sectors",
        action="store_true",
        help="Run sector digest for all active sectors in registry.",
    )
    p.add_argument(
        "--exclude-sectors",
        type=str,
        default="unknown,non_equity",
        metavar="CSV",
        help="Comma-separated sector ids to skip when using --all-sectors (default: unknown,non_equity).",
    )
    p.add_argument(
        "--sector-per-symbol-narrative",
        action="store_true",
        help="One Gemini call per symbol (high quota). Default is one digest call for the whole sector.",
    )
    p.add_argument(
        "--sector-digest",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--macro-json",
        type=str,
        default="",
        metavar="PATH",
        help=(
            "Optional JSON snapshot for macro guardrails "
            "(gift_nifty_change_pct, india_vix, dxy, lme_copper_change_pct)"
        ),
    )
    p.add_argument(
        "--events-json",
        type=str,
        default="",
        metavar="PATH",
        help="Optional JSON event snapshot (events: [{symbol,date,type,...}]).",
    )
    p.add_argument(
        "--protocol-run",
        action="store_true",
        help="Execute Titan V12 protocol windows/clusters using preset universes.",
    )
    p.add_argument(
        "--protocol-window",
        type=str,
        choices=("open", "mid", "cluster0"),
        default="",
        help="Restrict --protocol-run to one window.",
    )
    p.add_argument(
        "--protocol-clusters",
        type=str,
        default="",
        metavar="CSV",
        help=f"Comma list of protocol clusters (allowed: {', '.join(available_clusters())}).",
    )
    p.add_argument(
        "--strict-window",
        action="store_true",
        help="When set with --protocol-run, only execute windows that are due right now (IST).",
    )
    args = p.parse_args()

    macro_snapshot = _load_macro_snapshot(args.macro_json.strip()) if args.macro_json.strip() else None
    event_snapshot = _load_event_snapshot(args.events_json.strip()) if args.events_json.strip() else None
    protocol_clusters = _parse_cluster_list(args.protocol_clusters)
    exclude_sectors = _parse_csv_list(args.exclude_sectors)

    if args.protocol_run:
        try:
            run_protocol_window(
                window=(args.protocol_window.strip() or None),
                clusters=protocol_clusters,
                strict_window=args.strict_window,
                macro_snapshot=macro_snapshot,
                event_snapshot=event_snapshot,
                max_workers=args.sector_workers,
                max_symbols=args.sector_max_symbols,
            )
        except Exception:
            summary = traceback.format_exc().strip().splitlines()[-1][:180]
            send_failure_email(summary, detail=traceback.format_exc())
            raise
        return

    if args.sector.strip():
        from sector_audit import run_sector_live

        # --sector-digest kept for backward compatibility (no-op; digest is default).
        sector_digest = not args.sector_per_symbol_narrative
        run_kwargs = dict(
            max_workers=args.sector_workers,
            max_symbols=args.sector_max_symbols,
            digest=sector_digest,
        )
        if macro_snapshot is not None:
            run_kwargs["macro_snapshot"] = macro_snapshot
        if event_snapshot is not None:
            run_kwargs["event_snapshot"] = event_snapshot

        try:
            run_sector_live(
                args.sector.strip(),
                **run_kwargs,
            )
        except Exception as e:
            summary = str(e).strip().split("\n", 1)[0].strip()
            if len(summary) > 180:
                summary = summary[:177] + "..."
            send_failure_email(summary, detail=traceback.format_exc())
            raise
        return

    if args.all_sectors:
        try:
            run_all_sectors(
                max_workers=args.sector_workers,
                max_symbols=args.sector_max_symbols,
                digest=not args.sector_per_symbol_narrative,
                exclude_sectors=exclude_sectors,
                macro_snapshot=macro_snapshot,
                event_snapshot=event_snapshot,
            )
        except Exception as e:
            summary = str(e).strip().split("\n", 1)[0].strip()
            if len(summary) > 180:
                summary = summary[:177] + "..."
            send_failure_email(summary, detail=traceback.format_exc())
            raise
        return

    if args.live:
        try:
            run_live()
        except Exception as e:
            summary = str(e).strip().split("\n", 1)[0].strip()
            if len(summary) > 180:
                summary = summary[:177] + "..."
            send_failure_email(summary, detail=traceback.format_exc())
            raise
    else:
        run_dry_run()


if __name__ == "__main__":
    main()

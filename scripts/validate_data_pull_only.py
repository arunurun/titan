from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock, local
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
load_dotenv(ROOT / ".env", override=True)

from breeze_client import create_breeze_session, fetch_equity_data
from config_loader import load_config
from sector_registry import SectorInstrument, list_active_sector_ids, load_sector_instruments

_TLS = local()


def _thread_breeze(cfg: Any) -> Any:
    breeze = getattr(_TLS, "breeze", None)
    token = getattr(_TLS, "token", None)
    if breeze is None or token != cfg.breeze_session_token:
        breeze = create_breeze_session(cfg)
        _TLS.breeze = breeze
        _TLS.token = cfg.breeze_session_token
    return breeze


def _fetch_one(cfg: Any, inst: SectorInstrument) -> dict[str, Any]:
    try:
        df = fetch_equity_data(
            cfg,
            inst.symbol,
            inst.exchange,
            breeze=_thread_breeze(cfg),
            lookback_calendar_days=60,
            max_retries=3,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "error": str(exc),
        }
    if df.empty:
        return {
            "status": "no_data",
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "exchange_used": str(df.attrs.get("exchange_used", inst.exchange)),
            "fallback_used": bool(df.attrs.get("exchange_fallback_used", False)),
            "rows": 0,
        }
    return {
        "status": "ok",
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "exchange_used": str(df.attrs.get("exchange_used", inst.exchange)),
        "fallback_used": bool(df.attrs.get("exchange_fallback_used", False)),
        "rows": int(len(df)),
    }


def _sector_run(cfg: Any, sector: str, workers: int) -> dict[str, Any]:
    instruments = load_sector_instruments(sector)
    rows: list[dict[str, Any]] = []
    lock = Lock()
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(_fetch_one, cfg, inst) for inst in instruments]
        for fut in as_completed(futures):
            res = fut.result()
            with lock:
                rows.append(res)

    ok_rows = [r for r in rows if r["status"] == "ok"]
    no_data_rows = [r for r in rows if r["status"] == "no_data"]
    error_rows = [r for r in rows if r["status"] == "error"]
    fallback_rows = [r for r in ok_rows if r.get("fallback_used")]
    return {
        "sector": sector,
        "requested": len(rows),
        "ok": len(ok_rows),
        "no_data": len(no_data_rows),
        "errors": len(error_rows),
        "fallback_rescued": len(fallback_rows),
        "coverage_pct": round((len(ok_rows) / len(rows) * 100.0), 2) if rows else 0.0,
        "error_samples": error_rows[:10],
        "no_data_samples": no_data_rows[:15],
    }


def main() -> int:
    cfg = load_config()
    sectors = list_active_sector_ids(include_unknown=False)
    sectors = [s for s in sectors if s not in {"unknown", "non_equity"}]

    report: dict[str, Any] = {
        "run_started_at": datetime.now().isoformat(),
        "sector_count": len(sectors),
        "sectors": [],
    }
    print(f"[pull-validate] sectors={len(sectors)}")
    for idx, sector in enumerate(sectors, start=1):
        print(f"[pull-validate] ({idx}/{len(sectors)}) {sector}")
        res = _sector_run(cfg, sector, workers=1)
        report["sectors"].append(res)
        print(
            f"[pull-validate] {sector}: ok={res['ok']}/{res['requested']} "
            f"no_data={res['no_data']} errors={res['errors']} fallback_rescued={res['fallback_rescued']}"
        )

    report["run_finished_at"] = datetime.now().isoformat()
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "live_pull_validation.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[pull-validate] report={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Runtime helpers for Titan Protocol V12 execution windows and cluster presets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sector_registry import SectorInstrument

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class ProtocolRun:
    window: str
    cluster_id: str
    sector_id: str
    instruments: tuple[SectorInstrument, ...]


def _sym(x: str) -> str:
    return "".join(ch for ch in x.upper().strip() if ch.isalnum())


_CLUSTER_SYMBOLS: dict[str, tuple[str, ...]] = {
    "cluster0": ("NIFTY", "BANKNIFTY", "FINNIFTY"),
    "clustera": ("HAL", "MAZDOCK", "DATAPATTNS", "RVNL", "ZENTEC"),
    "clusterb": ("NTPC", "ADANIPOWER", "KPIGREEN", "PFC", "RECLTD"),
    "clusterc": ("ICICIBANK", "KOTAKBANK", "JIOFIN", "CHOLAFIN"),
    "clusterd": ("E2E", "NETWEB", "KAYNES", "GOLDIAM"),
    "clustere": ("HINDALCO", "HINDCOPPER", "COALINDIA", "GOODLUCK"),
    "clusterf": ("GOLDBEES", "BANKBEES", "CPSEETF", "NETF"),
}

_WINDOW_DEFAULTS: dict[str, tuple[str, ...]] = {
    "open": ("clustera", "clusterb", "clusterc", "clusterd", "clustere", "clusterf"),
    "mid": ("clustera", "clusterb", "clusterc", "clusterd", "clustere", "clusterf"),
    "cluster0": ("cluster0",),
}


def available_clusters() -> tuple[str, ...]:
    return tuple(sorted(_CLUSTER_SYMBOLS.keys()))


def cluster_instruments(cluster_id: str) -> tuple[SectorInstrument, ...]:
    cid = _sym(cluster_id).lower()
    syms = _CLUSTER_SYMBOLS.get(cid)
    if syms is None:
        raise ValueError(f"Unknown protocol cluster: {cluster_id!r}")
    return tuple(SectorInstrument(symbol=s, exchange="NSE") for s in syms)


def cluster_sector_id(cluster_id: str) -> str:
    return _sym(cluster_id).lower()


def should_run_window_now(
    window: str,
    *,
    now_ist: datetime | None = None,
    tolerance_minutes: int = 5,
) -> bool:
    if now_ist is None:
        now = datetime.now(IST)
    elif now_ist.tzinfo is None:
        now = now_ist.replace(tzinfo=IST)
    else:
        now = now_ist.astimezone(IST)
    if now.weekday() >= 5:
        return False
    hhmm = now.time()
    if window == "open":
        target = time(hour=9, minute=15)
        diff = abs((now.hour * 60 + now.minute) - (target.hour * 60 + target.minute))
        return diff <= max(0, int(tolerance_minutes))
    if window == "mid":
        target = time(hour=11, minute=30)
        diff = abs((now.hour * 60 + now.minute) - (target.hour * 60 + target.minute))
        return diff <= max(0, int(tolerance_minutes))
    if window == "cluster0":
        if hhmm < time(hour=9, minute=15) or hhmm > time(hour=15, minute=30):
            return False
        return now.minute % 30 == 0
    raise ValueError(f"Unknown window: {window!r}")


def resolve_protocol_runs(
    *,
    window: str | None = None,
    clusters: tuple[str, ...] | None = None,
    now_ist: datetime | None = None,
    strict_window: bool = False,
) -> list[ProtocolRun]:
    windows = (window,) if window else ("open", "mid", "cluster0")
    out: list[ProtocolRun] = []
    for w in windows:
        if strict_window and not should_run_window_now(w, now_ist=now_ist):
            continue
        cluster_ids = clusters or _WINDOW_DEFAULTS.get(w, ())
        for cid in cluster_ids:
            out.append(
                ProtocolRun(
                    window=w,
                    cluster_id=cid,
                    sector_id=cluster_sector_id(cid),
                    instruments=cluster_instruments(cid),
                )
            )
    return out


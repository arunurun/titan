"""Helpers for protocol scheduler loop (slot dedupe + command building)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from protocol_runtime import IST, should_run_window_now


def due_windows(
    *,
    now_ist: datetime | None = None,
    windows: tuple[str, ...] = ("open", "mid", "cluster0"),
    open_mid_tolerance_minutes: int = 0,
) -> list[str]:
    now = now_ist.astimezone(IST) if now_ist is not None else datetime.now(IST)
    out: list[str] = []
    for w in windows:
        tol = open_mid_tolerance_minutes if w in ("open", "mid") else 0
        if should_run_window_now(w, now_ist=now, tolerance_minutes=tol):
            out.append(w)
    return out


def window_slot_key(window: str, now_ist: datetime) -> str:
    now = now_ist.astimezone(IST)
    day = now.strftime("%Y-%m-%d")
    if window in ("open", "mid"):
        return f"{day}:{window}"
    if window == "cluster0":
        mm = (now.minute // 30) * 30
        return f"{day}:{window}:{now.hour:02d}:{mm:02d}"
    raise ValueError(f"Unknown window: {window!r}")


def build_protocol_command(
    *,
    python_exe: str,
    root: Path,
    window: str,
    clusters_csv: str = "",
    macro_json: str = "",
    events_json: str = "",
    sector_workers: int | None = None,
    sector_max_symbols: int | None = None,
) -> list[str]:
    cmd = [
        python_exe,
        str(root / "main.py"),
        "--protocol-run",
        "--protocol-window",
        window,
        "--strict-window",
    ]
    if clusters_csv.strip():
        cmd.extend(["--protocol-clusters", clusters_csv.strip()])
    if macro_json.strip():
        cmd.extend(["--macro-json", macro_json.strip()])
    if events_json.strip():
        cmd.extend(["--events-json", events_json.strip()])
    if sector_workers is not None:
        cmd.extend(["--sector-workers", str(int(sector_workers))])
    if sector_max_symbols is not None:
        cmd.extend(["--sector-max-symbols", str(int(sector_max_symbols))])
    return cmd


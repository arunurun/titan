#!/usr/bin/env python3
"""Run Titan V12 protocol windows in a resilient loop with lockfile dedupe."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from protocol_loop import build_protocol_command, due_windows, window_slot_key  # noqa: E402
from protocol_runtime import IST  # noqa: E402

logger = logging.getLogger("titan.protocol.loop")


def _acquire_lock(lock_path: Path, stale_minutes: int) -> int:
    now = datetime.now(IST)
    if lock_path.exists():
        try:
            txt = lock_path.read_text(encoding="utf-8").strip()
            ts = datetime.fromisoformat(txt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
        except Exception:
            ts = now
        if now - ts > timedelta(minutes=max(1, stale_minutes)):
            try:
                lock_path.unlink()
            except Exception:
                pass
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(lock_path), flags)
    os.write(fd, datetime.now(IST).isoformat(timespec="seconds").encode("utf-8"))
    return fd


def _release_lock(fd: int, lock_path: Path) -> None:
    try:
        os.close(fd)
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _run_window(
    window: str,
    *,
    clusters_csv: str,
    macro_json: str,
    events_json: str,
    sector_workers: int | None,
    sector_max_symbols: int | None,
) -> int:
    cmd = build_protocol_command(
        python_exe=sys.executable,
        root=ROOT,
        window=window,
        clusters_csv=clusters_csv,
        macro_json=macro_json,
        events_json=events_json,
        sector_workers=sector_workers,
        sector_max_symbols=sector_max_symbols,
    )
    logger.info("Executing protocol window=%s", window)
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return int(proc.returncode)


def main() -> None:
    p = argparse.ArgumentParser(description="Titan Protocol V12 scheduler loop")
    p.add_argument("--poll-seconds", type=int, default=30, help="Polling interval in seconds")
    p.add_argument(
        "--windows",
        type=str,
        default="open,mid,cluster0",
        help="Comma-separated windows to monitor (open,mid,cluster0)",
    )
    p.add_argument("--clusters", type=str, default="", help="Optional cluster CSV filter")
    p.add_argument("--macro-json", type=str, default="", help="Optional macro JSON snapshot path")
    p.add_argument("--events-json", type=str, default="", help="Optional events JSON snapshot path")
    p.add_argument("--sector-workers", type=int, default=None)
    p.add_argument("--sector-max-symbols", type=int, default=None)
    p.add_argument(
        "--open-mid-tolerance-minutes",
        type=int,
        default=0,
        help="Tolerance for open/mid due check (default exact minute).",
    )
    p.add_argument("--once", action="store_true", help="Check windows once and exit")
    p.add_argument(
        "--lock-file",
        type=str,
        default=str(ROOT / ".titan_protocol_loop.lock"),
        help="Lock file path to prevent duplicate loops",
    )
    p.add_argument("--stale-lock-minutes", type=int, default=240)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    windows = tuple(x.strip() for x in args.windows.split(",") if x.strip())
    seen_slots: set[str] = set()
    lock_path = Path(args.lock_file).expanduser()

    try:
        lock_fd = _acquire_lock(lock_path, args.stale_lock_minutes)
    except FileExistsError:
        raise SystemExit(f"Another protocol loop appears active (lock exists): {lock_path}")

    try:
        while True:
            now = datetime.now(IST)
            due = due_windows(
                now_ist=now,
                windows=windows,
                open_mid_tolerance_minutes=max(0, int(args.open_mid_tolerance_minutes)),
            )
            for window in due:
                slot = window_slot_key(window, now)
                if slot in seen_slots:
                    continue
                rc = _run_window(
                    window,
                    clusters_csv=args.clusters,
                    macro_json=args.macro_json,
                    events_json=args.events_json,
                    sector_workers=args.sector_workers,
                    sector_max_symbols=args.sector_max_symbols,
                )
                seen_slots.add(slot)
                if rc != 0:
                    logger.error("Protocol window %s failed with code %s", window, rc)
            if args.once:
                break
            time.sleep(max(5, int(args.poll_seconds)))
    finally:
        _release_lock(lock_fd, lock_path)


if __name__ == "__main__":
    main()


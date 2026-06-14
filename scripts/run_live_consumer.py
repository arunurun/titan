#!/usr/bin/env python3
"""Entry point for the Phase-1 live regime stream consumer (shadow mode).

The process must stay alive while subscribed — see module docstring in
``src/live_stream_consumer.py`` for env vars and setup steps.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config_loader import load_config  # noqa: E402
from live_stream_consumer import (  # noqa: E402
    LiveStreamConsumer,
    configure_logging,
    install_signal_handlers,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Titan live index regime consumer (Phase 1 skeleton)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and optionally persist one snapshot batch without opening the websocket",
    )
    args = parser.parse_args()

    configure_logging()
    try:
        cfg = load_config(require_gemini=False)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    consumer = LiveStreamConsumer(config=cfg)
    install_signal_handlers(consumer)

    if args.dry_run:
        consumer.run_dry_cycle()
        return 0

    consumer.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

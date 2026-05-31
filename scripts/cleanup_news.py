#!/usr/bin/env python3
"""Prune stale rows from news_feed.

Usage:
  python scripts/cleanup_news.py --older-than-hours 72
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from news_config import prepare_news_script_config
from news_store import cleanup_old_news

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete news_feed rows older than N hours.")
    parser.add_argument(
        "--older-than-hours",
        type=int,
        default=72,
        help="Retention window in hours (default 72).",
    )
    args = parser.parse_args()

    cfg = prepare_news_script_config()
    result = cleanup_old_news(cfg, older_than_hours=max(1, int(args.older_than_hours)))
    deleted = int(result.get("deleted") or 0)
    logger.info("Deleted %s news_feed row(s) older than %s hours", deleted, args.older_than_hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

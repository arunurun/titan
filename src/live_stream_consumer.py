"""
Live stream consumer — Phase 1 skeleton (shadow mode)
=====================================================

Persistent Breeze websocket consumer that subscribes to index quote streams,
derives stub regime states, and batch-writes ``live_regime_snapshots`` to
Supabase. Runs standalone; the EOD scoring path is unchanged until Phase 1-d
wires shadow reads.

Run locally (from repo root, during market hours 09:08–15:35 IST)
-----------------------------------------------------------------
1. Refresh the daily Breeze session token::

       python scripts/breeze_session.py

2. Apply DDL once (optional; needs ``SUPABASE_ACCESS_TOKEN``)::

       python scripts/apply_live_tables_migration.py

   Or paste ``sql/create_live_tables.sql`` into the Supabase SQL editor.

3. Start the consumer (process must stay alive — the SDK closes the socket on exit)::

       python scripts/run_live_consumer.py

   Debug one idle/market cycle without blocking forever::

       python scripts/run_live_consumer.py --dry-run

Required environment variables
------------------------------
- ``BREEZE_API_KEY``, ``BREEZE_SECRET``, ``BREEZE_SESSION_TOKEN`` — ICICI Breeze auth
- ``SUPABASE_URL``, ``SUPABASE_KEY`` — regime snapshot persistence

Optional tuning
---------------
- ``LIVE_SNAPSHOT_INTERVAL_SECONDS`` — regime batch write cadence (default ``60``)
- ``LIVE_HEARTBEAT_TIMEOUT_SECONDS`` — no-tick reconnect threshold in session (default ``30``)
- ``LIVE_RECONNECT_BACKOFF_BASE_SECONDS`` — reconnect backoff base (default ``2``)
- ``LIVE_REGIME_WRITE_ENABLED`` — ``0``/``false`` to log snapshots without Supabase writes
- ``MARKET_HOLIDAYS_IST`` — comma-separated ``YYYY-MM-DD`` dates (same as EOD path)

Do **not** set ``TITAN_RECONCILE_MODE`` — Breeze calls are blocked in reconcile mode.

Stub vs complete (Phase 1 skeleton)
-------------------------------------
- **Complete:** market-hours gating, daily session refresh hook, index quote subscription,
  ``on_ticks`` handling, batched Supabase writes, graceful shutdown.
- **Stub:** regime thresholds (placeholder % rules), slope_proxy, heartbeat reconnect
  (disconnect/reconnect without full backoff polish), no per-name quotes, no gap guard,
  no EOD gate reads — shadow-only persistence.
"""

from __future__ import annotations

import logging
import math
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from breeze_connect import BreezeConnect
from postgrest.exceptions import APIError
from supabase import create_client

from breeze_client import create_breeze_session
from config_loader import TitanConfig, load_config
from json_util import sanitize_for_json
from market_calendar import market_closed_reason_ist

IST = ZoneInfo("Asia/Kolkata")
LIVE_SESSION_START = dt_time(9, 8)
LIVE_SESSION_END = dt_time(15, 35)

REGIME_STATES = frozenset({"risk_on", "neutral", "risk_off", "rolling_over"})

# Indices subscribed in Phase 1 (regime set only).
REGIME_INDEX_SUBSCRIPTIONS: tuple[dict[str, str], ...] = (
    {"stock_code": "NIFTY", "index_code": "NIFTY"},
    {"stock_code": "CNXBAN", "index_code": "NIFTY BANK"},
)

logger = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [live] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("APILogger").setLevel(logging.INFO)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _normalize_quote_number(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def is_live_consumer_window_ist(now_ist: datetime | None = None) -> bool:
    """True on NSE cash weekdays during 09:08–15:35 IST (live consumer window)."""
    now = now_ist or datetime.now(IST)
    if market_closed_reason_ist(now) is not None:
        return False
    t = now.time()
    return LIVE_SESSION_START <= t <= LIVE_SESSION_END


@dataclass
class IndexQuoteState:
    index_code: str
    last: float | None = None
    open: float | None = None
    prev_close: float | None = None
    change: float | None = None
    last_tick_at: datetime | None = None
    prior_regime: str = "neutral"


@dataclass
class LiveStreamConsumer:
    """Phase-1 shadow consumer: index quotes → stub regime → Supabase snapshots."""

    config: TitanConfig
    snapshot_interval_seconds: float = field(default_factory=lambda: _env_float("LIVE_SNAPSHOT_INTERVAL_SECONDS", 60.0))
    heartbeat_timeout_seconds: float = field(default_factory=lambda: _env_float("LIVE_HEARTBEAT_TIMEOUT_SECONDS", 30.0))
    reconnect_backoff_base_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_RECONNECT_BACKOFF_BASE_SECONDS", 2.0)
    )
    regime_write_enabled: bool = field(default_factory=lambda: _env_bool("LIVE_REGIME_WRITE_ENABLED", True))

    _breeze: BreezeConnect | None = field(default=None, init=False, repr=False)
    _session_date: date | None = field(default=None, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _last_tick_at: datetime | None = field(default=None, init=False, repr=False)
    _last_snapshot_at: float = field(default=0.0, init=False, repr=False)
    _reconnect_attempts: int = field(default=0, init=False, repr=False)
    _index_states: dict[str, IndexQuoteState] = field(default_factory=dict, init=False, repr=False)
    _tick_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _writer_thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        for sub in REGIME_INDEX_SUBSCRIPTIONS:
            code = sub["index_code"]
            self._index_states[code] = IndexQuoteState(index_code=code)

    @property
    def breeze(self) -> BreezeConnect:
        if self._breeze is None:
            raise RuntimeError("Breeze session not initialized")
        return self._breeze

    def request_stop(self) -> None:
        self._stop_event.set()

    def _in_live_window(self) -> bool:
        return is_live_consumer_window_ist()

    def _ensure_daily_session(self) -> None:
        """Refresh Breeze REST/WS session once per IST calendar day."""
        today = datetime.now(IST).date()
        if self._session_date == today and self._breeze is not None:
            return
        logger.info("Refreshing Breeze session for trading day %s", today.isoformat())
        self._disconnect_ws()
        try:
            self._breeze = create_breeze_session(self.config)
            self._breeze.on_ticks = self._on_ticks
            self._session_date = today
            self._reconnect_attempts = 0
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "session" in msg and "expired" in msg:
                logger.error(
                    "Breeze session expired — halt consumer. Run: python scripts/breeze_session.py"
                )
                self.request_stop()
                raise
            raise

    def _subscribe_indices(self) -> None:
        for sub in REGIME_INDEX_SUBSCRIPTIONS:
            stock_code = sub["stock_code"]
            logger.info("Subscribing index quote stream: %s (%s)", sub["index_code"], stock_code)
            self.breeze.subscribe_feeds(
                exchange_code="NSE",
                stock_code=stock_code,
                product_type="cash",
                get_market_depth=False,
                get_exchange_quotes=True,
            )

    def _connect_and_subscribe(self) -> None:
        self._ensure_daily_session()
        logger.info("Opening Breeze websocket")
        self.breeze.ws_connect()
        self._connected = True
        self._last_tick_at = datetime.now(IST)
        self._subscribe_indices()
        self._reconnect_attempts = 0
        logger.info("Websocket connected; subscribed to %d index quote feeds", len(REGIME_INDEX_SUBSCRIPTIONS))

    def _disconnect_ws(self) -> None:
        if self._breeze is None:
            self._connected = False
            return
        try:
            self.breeze.ws_disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ws_disconnect ignored: %s", exc)
        self._connected = False

    def _reconnect_stub(self) -> None:
        """Heartbeat-triggered reconnect with exponential backoff (stub)."""
        self._reconnect_attempts += 1
        backoff = min(
            120.0,
            self.reconnect_backoff_base_seconds * (2 ** min(self._reconnect_attempts - 1, 6)),
        )
        logger.warning(
            "Heartbeat stale — reconnect attempt %s after %.1fs backoff",
            self._reconnect_attempts,
            backoff,
        )
        self._disconnect_ws()
        time.sleep(backoff)
        if self._stop_event.is_set() or not self._in_live_window():
            return
        try:
            self._ensure_daily_session()
            self._connect_and_subscribe()
        except Exception as exc:  # noqa: BLE001
            logger.error("Reconnect failed: %s", exc)

    def _check_heartbeat(self) -> None:
        if not self._connected or not self._in_live_window():
            return
        if self._last_tick_at is None:
            return
        age = (datetime.now(IST) - self._last_tick_at).total_seconds()
        if age >= self.heartbeat_timeout_seconds:
            self._reconnect_stub()

    def _resolve_index_code(self, tick: dict[str, Any]) -> str | None:
        symbol = str(tick.get("symbol") or tick.get("stock_code") or "").strip().upper()
        for sub in REGIME_INDEX_SUBSCRIPTIONS:
            if symbol in {sub["stock_code"].upper(), sub["index_code"].upper()}:
                return sub["index_code"]
            if symbol.replace(" ", "") in {"NIFTY50", "NIFTY"} and sub["index_code"] == "NIFTY":
                return sub["index_code"]
            if symbol in {"BANKNIFTY", "NIFTYBANK", "CNXBAN"} and sub["index_code"] == "NIFTY BANK":
                return sub["index_code"]
        return None

    def _on_ticks(self, ticks: Any) -> None:
        if ticks is None:
            return
        packets = ticks if isinstance(ticks, list) else [ticks]
        now = datetime.now(IST)
        with self._tick_lock:
            self._last_tick_at = now
            for tick in packets:
                if not isinstance(tick, dict):
                    continue
                index_code = self._resolve_index_code(tick)
                if index_code is None:
                    continue
                state = self._index_states[index_code]
                last = _normalize_quote_number(tick.get("last") or tick.get("ltp"))
                open_px = _normalize_quote_number(tick.get("open"))
                close_px = _normalize_quote_number(tick.get("close") or tick.get("previous_close"))
                change = _normalize_quote_number(tick.get("change"))
                if last is not None:
                    state.last = last
                if open_px is not None:
                    state.open = open_px
                if close_px is not None:
                    state.prev_close = close_px
                if change is not None:
                    state.change = change
                state.last_tick_at = now

    def derive_regime_state(self, state: IndexQuoteState) -> dict[str, Any]:
        """
        Stub regime classifier — placeholder thresholds for Phase 1-b calibration.

        Returns snapshot fields: last, pct_vs_prev_close, pct_vs_open, slope_proxy, regime_state.
        """
        last = state.last
        open_px = state.open
        prev_close = state.prev_close

        pct_vs_prev_close: float | None = None
        if last is not None and prev_close not in (None, 0.0):
            pct_vs_prev_close = (last - prev_close) / prev_close * 100.0
        elif state.change is not None:
            pct_vs_prev_close = state.change

        pct_vs_open: float | None = None
        if last is not None and open_px not in (None, 0.0):
            pct_vs_open = (last - open_px) / open_px * 100.0

        slope_proxy: float | None = None
        if pct_vs_open is not None and pct_vs_prev_close is not None:
            slope_proxy = pct_vs_open - (0.5 * pct_vs_prev_close)

        regime = "neutral"
        if pct_vs_prev_close is not None and pct_vs_open is not None:
            if pct_vs_open > 0.05 and pct_vs_prev_close < -0.10:
                regime = "rolling_over"
            elif pct_vs_prev_close >= 0.25 and pct_vs_open >= 0.0:
                regime = "risk_on"
            elif pct_vs_prev_close <= -0.25 and pct_vs_open <= 0.0:
                regime = "risk_off"
            elif state.prior_regime == "risk_on" and pct_vs_open < -0.15:
                regime = "rolling_over"

        state.prior_regime = regime
        return {
            "index_code": state.index_code,
            "last": last,
            "pct_vs_prev_close": pct_vs_prev_close,
            "pct_vs_open": pct_vs_open,
            "slope_proxy": slope_proxy,
            "regime_state": regime,
        }

    def build_regime_snapshot_rows(self, snapshot_ts: datetime) -> list[dict[str, Any]]:
        ts_iso = snapshot_ts.isoformat(timespec="seconds")
        rows: list[dict[str, Any]] = []
        with self._tick_lock:
            for state in self._index_states.values():
                derived = self.derive_regime_state(state)
                if derived["regime_state"] not in REGIME_STATES:
                    derived["regime_state"] = "neutral"
                rows.append(
                    sanitize_for_json(
                        {
                            "snapshot_ts": ts_iso,
                            "index_code": derived["index_code"],
                            "last": derived["last"],
                            "pct_vs_prev_close": derived["pct_vs_prev_close"],
                            "pct_vs_open": derived["pct_vs_open"],
                            "slope_proxy": derived["slope_proxy"],
                            "regime_state": derived["regime_state"],
                            "source": "breeze_ws_quote",
                        }
                    )
                )
        return rows

    def persist_regime_snapshots(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if not self.regime_write_enabled:
            logger.info("LIVE_REGIME_WRITE_ENABLED=off — would persist %d row(s): %s", len(rows), rows)
            return
        client = create_client(self.config.supabase_url, self.config.supabase_key)
        try:
            client.table("live_regime_snapshots").insert(rows).execute()
            logger.info("Persisted %d live_regime_snapshots row(s)", len(rows))
        except APIError as exc:
            payload = exc.args[0] if exc.args else {}
            code = payload.get("code", "") if isinstance(payload, dict) else ""
            msg = payload.get("message", str(exc)) if isinstance(payload, dict) else str(exc)
            if code == "PGRST205" or "could not find the table" in msg.lower():
                raise RuntimeError(
                    "[Supabase] Table missing: public.live_regime_snapshots. "
                    "Run scripts/apply_live_tables_migration.py or sql/create_live_tables.sql."
                ) from exc
            raise RuntimeError(f"[Supabase] live_regime_snapshots insert failed ({code}): {msg}") from exc

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=1.0):
                break
            if not self._in_live_window():
                continue
            now_mono = time.monotonic()
            if now_mono - self._last_snapshot_at < self.snapshot_interval_seconds:
                continue
            self._last_snapshot_at = now_mono
            snapshot_ts = datetime.now(IST)
            rows = self.build_regime_snapshot_rows(snapshot_ts)
            try:
                self.persist_regime_snapshots(rows)
            except Exception as exc:  # noqa: BLE001
                logger.error("Regime snapshot write failed: %s", exc)

    def _idle_outside_market(self) -> None:
        if self._connected:
            logger.info("Outside live window — disconnecting websocket")
            self._disconnect_ws()
        # Sleep until next window check (wake early on stop).
        for _ in range(30):
            if self._stop_event.is_set():
                return
            if self._in_live_window():
                return
            time.sleep(1)

    def run_forever(self) -> None:
        """Main loop — stays alive for the Breeze websocket SDK."""
        self._writer_thread = threading.Thread(target=self._writer_loop, name="live-regime-writer", daemon=True)
        self._writer_thread.start()
        logger.info(
            "Live consumer started (shadow mode). Window=%s–%s IST; snapshot every %.0fs",
            LIVE_SESSION_START.strftime("%H:%M"),
            LIVE_SESSION_END.strftime("%H:%M"),
            self.snapshot_interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                if not self._in_live_window():
                    self._idle_outside_market()
                    continue
                if not self._connected:
                    self._connect_and_subscribe()
                self._check_heartbeat()
            except RuntimeError as exc:
                if "session expired" in str(exc).lower():
                    break
                logger.error("Consumer loop error: %s", exc)
                time.sleep(self.reconnect_backoff_base_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected consumer error: %s", exc)
                time.sleep(self.reconnect_backoff_base_seconds)
            else:
                time.sleep(1.0)
        self._disconnect_ws()
        logger.info("Live consumer stopped")

    def run_dry_cycle(self) -> None:
        """One-shot path for local smoke checks without opening the websocket."""
        logger.info("Dry-run: building stub regime snapshot rows (no websocket)")
        rows = self.build_regime_snapshot_rows(datetime.now(IST))
        for row in rows:
            logger.info("Dry-run row: %s", row)
        self.persist_regime_snapshots(rows)


def install_signal_handlers(consumer: LiveStreamConsumer) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s — shutting down", signum)
        consumer.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass

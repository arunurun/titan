from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pandas as pd
from postgrest.exceptions import APIError

from config_loader import TitanConfig


def make_cfg() -> TitanConfig:
    return TitanConfig(
        breeze_api_key="k",
        breeze_secret="s",
        breeze_session_token="t",
        gemini_api_keys=("g",),
        supabase_url="https://x.supabase.co",
        supabase_key="sk",
    )


def test_bucket_thresholds():
    from sector_priority import _bucket_from_market_cap_cr

    assert _bucket_from_market_cap_cr(None) == "unknown"
    assert _bucket_from_market_cap_cr(1000.0) == "micro"
    assert _bucket_from_market_cap_cr(6000.0) == "small"
    assert _bucket_from_market_cap_cr(30000.0) == "mid"
    assert _bucket_from_market_cap_cr(80000.0) == "large"


def test_fetch_nse_market_cap_parses_rupees_payload(monkeypatch):
    from sector_priority import fetch_nse_market_cap_inr_cr

    monkeypatch.setattr(
        "sector_priority._fetch_nse_json",
        lambda _symbol: {"info": {"marketCap": 3_000_000_000_000}},
    )
    cap, source = fetch_nse_market_cap_inr_cr("INFY")
    assert cap == 300000.0
    assert source == "nse_quote_rupees"


def test_fetch_yahoo_market_cap_parses_rupees_payload(monkeypatch):
    from sector_priority import fetch_yahoo_market_cap_inr_cr

    class _Resp:
        def __init__(self, payload: str):
            self._payload = payload

        def read(self):
            return self._payload.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    payload = {
        "quoteResponse": {
            "result": [
                {
                    "symbol": "TECHM.NS",
                    "currency": "INR",
                    "marketCap": 1_000_000_000_000,
                }
            ]
        }
    }
    monkeypatch.setattr(
        "sector_priority.urllib.request.urlopen",
        lambda req, timeout=20.0: _Resp(__import__("json").dumps(payload)),
    )
    cap, source = fetch_yahoo_market_cap_inr_cr("TECHM", "NSE")
    assert cap == 100000.0
    assert source == "yahoo_quote_rupees"


def test_fetch_moneycontrol_market_cap_parses_html(monkeypatch):
    from sector_priority import fetch_moneycontrol_market_cap_inr_cr

    payload = [
        {"sc_id": "TECHM", "link_src": "https://www.moneycontrol.com/india/stockpricequote/computers-software/techmahindra/TM4"}
    ]
    html = "<td>Mkt Cap (Rs. Cr.)</td><td>1,23,456.78</td>"
    calls = {"i": 0}

    def _fake_get(url, timeout_seconds=20.0):
        calls["i"] += 1
        if calls["i"] == 1:
            return (__import__("json").dumps(payload), None)
        return (html, None)

    monkeypatch.setattr("sector_priority._http_get_text", _fake_get)
    cap, source = fetch_moneycontrol_market_cap_inr_cr("TECHM")
    assert cap == 123456.78
    assert source == "moneycontrol_quote_rs_cr"


def test_priority_selection_skips_zero_history_rows(monkeypatch):
    from sector_priority import build_sector_rankings
    from sector_registry import SectorInstrument

    cfg = make_cfg()
    instruments = [SectorInstrument("A", "NSE"), SectorInstrument("B", "NSE")]
    df_ok = __import__("pandas").DataFrame(
        {"close": [10, 11, 12, 13, 14, 15], "volume": [100] * 6}
    )
    df_empty = __import__("pandas").DataFrame()

    def _fake_fetch(cfg, symbol, exchange, **kwargs):
        return df_ok if symbol == "A" else df_empty

    monkeypatch.setattr("breeze_client.create_breeze_session", lambda _cfg: object())
    monkeypatch.setattr("sector_priority.fetch_equity_data", _fake_fetch)
    monkeypatch.setattr("sector_priority.fetch_nse_market_cap_inr_cr", lambda _sym: (None, "nse_quote_missing"))
    monkeypatch.setattr("sector_priority.fetch_moneycontrol_market_cap_inr_cr", lambda _sym: (None, "moneycontrol_quote_missing"))
    monkeypatch.setattr("sector_priority.fetch_screener_market_cap_inr_cr", lambda _sym: (None, "screener_quote_missing"))
    monkeypatch.setattr("sector_priority.fetch_yahoo_market_cap_inr_cr", lambda _s, _e: (None, "yahoo_quote_missing"))
    monkeypatch.setattr("sector_priority._load_previous_market_caps", lambda cfg, sector_key: {})

    rows = build_sector_rankings(cfg, sector_key="ai", instruments=instruments, top_n=2)
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["A"]["is_priority"] is True
    assert by_sym["B"]["is_priority"] is False


def test_load_priority_instruments(monkeypatch):
    from sector_priority import load_priority_instruments

    class _FakeQuery:
        def __init__(self, client: "_FakeClient"):
            self.client = client
            self.filters: dict[str, str] = {}
            self._limit: int | None = None
            self._order_field: str | None = None
            self._order_desc = False

        def select(self, _fields):
            return self

        def eq(self, key, value):
            self.filters[str(key)] = str(value)
            return self

        def order(self, field, desc=False):
            self._order_field = str(field)
            self._order_desc = bool(desc)
            return self

        def limit(self, n):
            self._limit = int(n)
            return self

        def execute(self):
            return SimpleNamespace(data=self.client._execute(self))

    class _FakeClient:
        def __init__(self, *, today_rows: list, latest_date: str, latest_rows: list):
            self.today_rows = today_rows
            self.latest_date = latest_date
            self.latest_rows = latest_rows

        def table(self, _name):
            return _FakeQuery(self)

        def _execute(self, query: _FakeQuery) -> list[dict]:
            if query._order_field == "as_of_date" and query._order_desc:
                if query.filters.get("is_priority") == "True":
                    return [{"as_of_date": self.latest_date}]
                return []
            as_of = query.filters.get("as_of_date", "")
            if as_of == self.latest_date:
                return list(self.latest_rows)
            return list(self.today_rows)

    monkeypatch.setattr(
        "sector_priority.create_client",
        lambda _u, _k: _FakeClient(
            today_rows=[],
            latest_date="2026-05-24",
            latest_rows=[
                {"symbol": "A", "exchange": "NSE", "rank_in_sector": 1},
                {"symbol": "B", "exchange": "BSE", "rank_in_sector": 2},
            ],
        ),
    )
    out = load_priority_instruments(make_cfg(), sector_key="ai", top_n=2)
    assert [f"{x.symbol}:{x.exchange}" for x in out] == ["A:NSE", "B:BSE"]


def test_load_priority_instruments_uses_today_when_present(monkeypatch):
    from sector_priority import load_priority_instruments

    class _FakeQuery:
        def __init__(self, payload):
            self.payload = payload

        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def order(self, _k, desc=False):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(data=self.payload)

    class _FakeClient:
        def __init__(self, payload):
            self.payload = payload

        def table(self, _name):
            return _FakeQuery(self.payload)

    monkeypatch.setattr(
        "sector_priority.create_client",
        lambda _u, _k: _FakeClient(
            [
                {"symbol": "HAL", "exchange": "NSE", "rank_in_sector": 1},
            ]
        ),
    )
    out = load_priority_instruments(make_cfg(), sector_key="defence", top_n=1)
    assert [f"{x.symbol}:{x.exchange}" for x in out] == ["HAL:NSE"]


def test_persist_daily_winners_builds_rows(monkeypatch):
    from sector_priority import persist_daily_winners

    captured: dict[str, object] = {}

    class _InsertQuery:
        def __init__(self, payload):
            self.payload = payload

        def eq(self, _k, _v):
            return self

        def execute(self):
            captured["upsert_payload"] = self.payload
            return SimpleNamespace(data=self.payload)

    class _ReadQuery:
        def __init__(self, payload):
            self.payload = payload

        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def order(self, _k):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(data=self.payload)

    class _FakeClient:
        def table(self, name):
            if name == "sector_priority_rankings":
                return _ReadQuery(
                    [
                        {
                            "symbol": "A",
                            "exchange": "NSE",
                            "rank_score": 10.5,
                            "market_cap_bucket": "small",
                            "return_1w_pct": 2.1,
                            "return_1m_pct": 4.2,
                            "absorption_ratio": 1.4,
                            "meta": {"issues": [], "rows_count": 40, "market_cap_source": "nse_quote_rupees"},
                            "rank_in_sector": 1,
                            "is_priority": True,
                        }
                    ]
                )
            if name == "sector_daily_winners":
                return SimpleNamespace(
                    delete=lambda: _InsertQuery([]),
                    upsert=lambda payload, on_conflict=None: _InsertQuery(payload)
                )
            raise AssertionError(f"unexpected table: {name}")

    monkeypatch.setattr("sector_priority.create_client", lambda _u, _k: _FakeClient())
    res = persist_daily_winners(make_cfg(), sector_key="ai", top_n=10)
    assert res["persisted"] is True
    payload = captured.get("upsert_payload")
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["symbol"] == "A"
    assert payload[0]["winner_rank"] == 1


def test_persist_daily_winners_repeated_runs_are_idempotent(monkeypatch):
    from sector_priority import persist_daily_winners

    state: dict[tuple[str, str, str, str], dict[str, object]] = {}

    class _WriteQuery:
        def __init__(self):
            self._delete_sector_key = ""
            self._delete_as_of_date = ""

        def delete(self):
            return self

        def upsert(self, payload, on_conflict=None):
            self._payload = payload
            return self

        def eq(self, key, value):
            if key == "sector_key":
                self._delete_sector_key = value
            if key == "as_of_date":
                self._delete_as_of_date = value
            return self

        def execute(self):
            if hasattr(self, "_payload"):
                for row in self._payload:
                    k = (
                        str(row["sector_key"]),
                        str(row["as_of_date"]),
                        str(row["symbol"]),
                        str(row["exchange"]),
                    )
                    if k in state:
                        raise APIError({"message": "duplicate key value violates unique constraint", "code": "23505"})
                    state[k] = row
                return SimpleNamespace(data=self._payload)
            state_keys = list(state.keys())
            for k in state_keys:
                if k[0] == self._delete_sector_key and k[1] == self._delete_as_of_date:
                    del state[k]
            return SimpleNamespace(data=[])

    class _ReadQuery:
        def __init__(self, payload):
            self.payload = payload

        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def order(self, _k):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(data=self.payload)

    class _FakeClient:
        def table(self, name):
            if name == "sector_priority_rankings":
                return _ReadQuery(
                    [
                        {
                            "symbol": "A",
                            "exchange": "NSE",
                            "rank_score": 10.5,
                            "market_cap_bucket": "small",
                            "return_1w_pct": 2.1,
                            "return_1m_pct": 4.2,
                            "absorption_ratio": 1.4,
                            "meta": {"issues": [], "rows_count": 40, "market_cap_source": "nse_quote_rupees"},
                            "rank_in_sector": 1,
                            "is_priority": True,
                        }
                    ]
                )
            if name == "sector_daily_winners":
                return _WriteQuery()
            raise AssertionError(f"unexpected table: {name}")

    monkeypatch.setattr("sector_priority.create_client", lambda _u, _k: _FakeClient())
    first = persist_daily_winners(make_cfg(), sector_key="ai", top_n=10)
    second = persist_daily_winners(make_cfg(), sector_key="ai", top_n=10)
    assert first["persisted"] is True
    assert second["persisted"] is True
    assert len(state) == 1


def test_fetch_latest_global_news_dedupes_and_drops_stale(monkeypatch):
    from sector_priority import fetch_latest_global_news

    monkeypatch.setenv("TITAN_NEWS_FEEDS", "https://feed-a.local/rss,https://feed-b.local/rss")
    monkeypatch.setenv("TITAN_NEWS_MAX_AGE_HOURS", "48")
    monkeypatch.setenv("TITAN_NEWS_FETCH_LIMIT", "20")

    feed_a = """<rss><channel><title>Feed A</title>
    <item><title>AI chip demand surges on cloud capex</title><link>https://x/a1</link>
    <pubDate>Tue, 02 Jan 2026 08:00:00 GMT</pubDate><description>Strong expansion signal</description></item>
    <item><title>Old stale headline</title><link>https://x/stale</link>
    <pubDate>Mon, 01 Dec 2025 08:00:00 GMT</pubDate><description>old</description></item>
    </channel></rss>"""
    feed_b = """<rss><channel><title>Feed B</title>
    <item><title>AI chip demand surges on cloud capex</title><link>https://x/a1</link>
    <pubDate>Tue, 02 Jan 2026 09:00:00 GMT</pubDate><description>Duplicate newer copy</description></item>
    </channel></rss>"""

    def _fake_get(url, timeout_seconds=12.0):
        if "feed-a" in url:
            return feed_a, None
        if "feed-b" in url:
            return feed_b, None
        return None, "not_found"

    monkeypatch.setattr("sector_priority._http_get_text", _fake_get)
    items = fetch_latest_global_news(now_utc=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc))
    assert len(items) == 1
    assert items[0]["title"].startswith("AI chip demand")
    assert items[0]["source"] == "Feed B"


def test_fetch_stock_news_for_symbol_uses_alias_fallback(monkeypatch):
    from sector_priority import fetch_stock_news_for_symbol

    class _Query:
        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(data=[{"symbol": "HAL", "instrument_name": "Hindustan Aeronautics", "breeze_stock_code": "HAL"}])

    class _Client:
        def table(self, _name):
            return _Query()

    monkeypatch.setattr("sector_priority.create_client", lambda _u, _k: _Client())
    monkeypatch.setattr(
        "sector_priority.fetch_nse_bulk_block_deals",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )
    monkeypatch.setattr(
        "sector_priority.fetch_nse_corporate_announcements",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )

    def _fake_get(url, timeout_seconds=10.0):
        if "Hindustan" in url:
            return (
                """<rss><channel><title>Google News</title>
                <item><title>Hindustan Aeronautics wins order</title><link>https://x/hal</link>
                <pubDate>Tue, 02 Jan 2026 09:00:00 GMT</pubDate><description>Order win</description></item>
                </channel></rss>""",
                None,
            )
        return "<rss><channel><title>Google News</title></channel></rss>", None

    monkeypatch.setattr("sector_priority._http_get_text", _fake_get)
    out = fetch_stock_news_for_symbol(
        make_cfg(),
        symbol="HAL",
        exchange="NSE",
        now_utc=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
    )
    assert out["items"]
    assert out["fallback_used"] is True
    assert "Hindustan Aeronautics" in out["alias_used"]


def test_filter_stock_news_rejects_hyundai_comparison():
    from sector_priority import _filter_stock_news_items

    items = [
        {
            "title": "Toyota vs Hyundai: which compact SUV is better in 2026?",
            "summary": "Comparison review",
            "source": "Google News",
            "url": "https://x/compare",
            "published_at": "2026-05-28T10:00:00+00:00",
        }
    ]
    kept, meta = _filter_stock_news_items(
        symbol="HYUNDAI",
        aliases=["Hyundai Motor India"],
        items=items,
    )
    assert kept == []
    assert meta["filtered_count"] == 1
    assert meta["rejection_samples"][0]["reason"].startswith("negative:")


def test_filter_stock_news_rejects_listicle():
    from sector_priority import _filter_stock_news_items

    items = [
        {
            "title": "5 stocks to buy today: Tube Investments, HAL, and more",
            "summary": "Broker picks",
            "source": "Investment Guru",
            "url": "https://x/list",
            "published_at": "2026-05-28T10:00:00+00:00",
        }
    ]
    kept, meta = _filter_stock_news_items(
        symbol="TIINDIA",
        aliases=["Tube Investments of India"],
        items=items,
    )
    assert kept == []
    assert meta["filtered_count"] == 1


def test_filter_stock_news_keeps_bulk_deal_headline():
    from sector_priority import _filter_stock_news_items

    items = [
        {
            "title": "HYUNDAI: Bulk deal — BUY 50000 @ 1500 (ABC Capital)",
            "summary": "NSE bulk deal for HYUNDAI",
            "source": "nse_bulk_deals",
            "url": "https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
            "published_at": "2026-05-28T10:00:00+00:00",
        }
    ]
    kept, meta = _filter_stock_news_items(
        symbol="HYUNDAI",
        aliases=["Hyundai Motor India"],
        items=items,
    )
    assert len(kept) == 1
    assert "Bulk deal" in kept[0]["title"]
    assert meta["filtered_count"] == 0


def test_correlate_stock_news_macro_fallback_when_all_rejected():
    from sector_priority import correlate_stock_news_with_macro

    snapshot = {
        "sector_scores": {
            "auto": {
                "score": 0.08,
                "confidence": 0.7,
                "drivers_top": [
                    {
                        "title": "Auto demand steady in India",
                        "source": "Reuters",
                        "published_at": "2026-01-02T08:00:00+00:00",
                        "contribution": 0.05,
                    }
                ],
            }
        }
    }
    stock_news_items = [
        {
            "title": "5 stocks to buy today including Hyundai",
            "summary": "Listicle",
            "source": "Yahoo Finance",
            "url": "https://x/list",
            "published_at": "2026-01-02T10:00:00+00:00",
        }
    ]
    out = correlate_stock_news_with_macro(
        symbol="HYUNDAI",
        sector_key="auto",
        stock_news_items=stock_news_items,
        snapshot=snapshot,
        aliases=["Hyundai Motor India"],
    )
    assert out["fallback_label"] == "stock_news_no_relevant_items"
    assert "Auto demand steady" in out["driver"]


def test_fetch_nse_bulk_block_deals_parses_payload(monkeypatch):
    from sector_priority import fetch_nse_bulk_block_deals

    def _fake_api(url, *, params=None, timeout_seconds=20.0):
        option = (params or {}).get("optionType", "")
        if option == "bulk_deals":
            return (
                [
                    {
                        "BD_DT_DATE": "28-MAY-2026",
                        "BD_SYMBOL": "HYUNDAI",
                        "BD_BUY_SELL": "BUY",
                        "BD_QTY_TRD": 50000,
                        "BD_TP_WATP": 1500.0,
                        "BD_CLIENT_NAME": "ABC Capital",
                    }
                ],
                "",
            )
        return ([], "nse_empty")

    monkeypatch.setattr("sector_priority._fetch_nse_api", _fake_api)
    out = fetch_nse_bulk_block_deals("HYUNDAI", now_utc=datetime(2026, 5, 30, tzinfo=timezone.utc))
    assert len(out["items"]) == 1
    assert "Bulk deal" in out["items"][0]["title"]
    assert out["items"][0]["source"] == "nse_bulk_deals"


def test_fetch_nse_corporate_announcements_parses_payload(monkeypatch):
    from sector_priority import fetch_nse_corporate_announcements

    monkeypatch.setattr(
        "sector_priority._fetch_nse_api",
        lambda url, *, params=None, timeout_seconds=20.0: (
            [
                {
                    "symbol": "HYUNDAI",
                    "desc": "Board meeting outcome",
                    "attchmntText": "Results approved",
                    "an_dt": "28-MAY-2026",
                }
            ],
            "",
        ),
    )
    out = fetch_nse_corporate_announcements("HYUNDAI", now_utc=datetime(2026, 5, 30, tzinfo=timezone.utc))
    assert len(out["items"]) == 1
    assert out["items"][0]["source"] == "nse_corporate_announcements"
    assert "Board meeting" in out["items"][0]["title"]


def test_fetch_nse_bulk_block_deals_graceful_error(monkeypatch):
    from sector_priority import fetch_nse_bulk_block_deals

    monkeypatch.setattr(
        "sector_priority._fetch_nse_api",
        lambda *_args, **_kwargs: (None, "nse_cookie_failed"),
    )
    out = fetch_nse_bulk_block_deals("HYUNDAI")
    assert out["items"] == []
    assert "nse_cookie_failed" in out["error"]


def test_correlate_stock_news_with_macro_prefers_stock_driver():
    from sector_priority import correlate_stock_news_with_macro

    snapshot = {
        "sector_scores": {
            "defence": {
                "score": 0.12,
                "confidence": 0.8,
                "drivers_top": [
                    {
                        "title": "Defence demand rises globally",
                        "source": "Reuters",
                        "published_at": "2026-01-02T08:00:00+00:00",
                        "contribution": 0.09,
                    }
                ],
            }
        }
    }
    stock_news_items = [
        {
            "title": "HAL signs major export contract",
            "summary": "Contract win boosts growth visibility",
            "source": "ET Markets",
            "url": "https://x/hal-contract",
            "published_at": "2026-01-02T10:00:00+00:00",
        }
    ]
    out = correlate_stock_news_with_macro(
        symbol="HAL",
        sector_key="defence",
        stock_news_items=stock_news_items,
        snapshot=snapshot,
    )
    assert out["driver"].startswith("HAL signs major export contract")
    assert out["fallback_label"] == ""
    top = out["evidence"]["top_headlines"]
    assert top["stock"]
    assert set(["global", "local", "market", "stock"]).issubset(set(top.keys()))


def test_stock_news_query_candidates_include_nse_suffix():
    from sector_priority import _stock_news_query_candidates

    primary, fallback = _stock_news_query_candidates(symbol="HAL", aliases=["Hindustan Aeronautics"])
    assert any("Hindustan Aeronautics" in q and "when:7d" in q for q in primary)
    assert any("HAL NSE when:7d" in q for q in primary)
    assert all("-recommend" in q for q in primary if "when:7d" in q)
    assert any("HAL stock India" in q for q in fallback)
    assert "HAL" in fallback


def test_fetch_stock_news_reports_all_filtered_when_items_rejected(monkeypatch):
    from sector_priority import fetch_stock_news_for_symbol

    class _Query:
        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(data=[])

    class _Client:
        def table(self, _name):
            return _Query()

    monkeypatch.setattr("sector_priority.create_client", lambda _u, _k: _Client())
    monkeypatch.setattr(
        "sector_priority.fetch_nse_bulk_block_deals",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_cookie_failed"},
    )
    monkeypatch.setattr(
        "sector_priority.fetch_nse_corporate_announcements",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )

    rss = """<rss><channel><title>Google News</title>
    <item><title>5 stocks to buy today including HAL</title><link>https://x/list</link>
    <pubDate>Tue, 02 Jan 2026 09:00:00 GMT</pubDate><description>Broker picks</description></item>
    </channel></rss>"""

    monkeypatch.setattr(
        "sector_priority._http_get_text",
        lambda url, timeout_seconds=10.0: (rss, None),
    )
    out = fetch_stock_news_for_symbol(
        make_cfg(),
        symbol="HAL",
        exchange="NSE",
        now_utc=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
    )
    assert out["items"] == []
    assert out["error"] == "all_filtered"
    assert out["rss_pre_filter_count"] >= 1
    assert out["filtered_count"] >= 1


def test_fetch_stock_news_uses_simple_fallback_when_primary_empty(monkeypatch):
    from sector_priority import fetch_stock_news_for_symbol

    class _Query:
        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(
                data=[{"symbol": "HAL", "instrument_name": "Hindustan Aeronautics", "breeze_stock_code": "HAL"}]
            )

    class _Client:
        def table(self, _name):
            return _Query()

    monkeypatch.setattr("sector_priority.create_client", lambda _u, _k: _Client())
    monkeypatch.setattr(
        "sector_priority.fetch_nse_bulk_block_deals",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )
    monkeypatch.setattr(
        "sector_priority.fetch_nse_corporate_announcements",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )

    def _fake_get(url, timeout_seconds=10.0):
        if "when%3A7d" in url or "when:7d" in url:
            return "<rss><channel><title>Google News</title></channel></rss>", None
        if "stock+India" in url or "Hindustan" in url:
            return (
                """<rss><channel><title>Google News</title>
                <item><title>Hindustan Aeronautics wins export order</title><link>https://x/hal</link>
                <pubDate>Tue, 02 Jan 2026 09:00:00 GMT</pubDate><description>Order win</description></item>
                </channel></rss>""",
                None,
            )
        return "<rss><channel><title>Google News</title></channel></rss>", None

    monkeypatch.setattr("sector_priority._http_get_text", _fake_get)
    out = fetch_stock_news_for_symbol(
        make_cfg(),
        symbol="HAL",
        exchange="NSE",
        now_utc=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
    )
    assert out["items"]
    assert out["fallback_used"] is True
    assert "stock India" in out["query_used"] or "Hindustan" in out["query_used"]


def test_fetch_stock_news_retries_simple_fallback_when_primary_filtered(monkeypatch):
    from sector_priority import fetch_stock_news_for_symbol

    class _Query:
        def select(self, _fields):
            return self

        def eq(self, _k, _v):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            return SimpleNamespace(
                data=[{"symbol": "HAL", "instrument_name": "Hindustan Aeronautics", "breeze_stock_code": "HAL"}]
            )

    class _Client:
        def table(self, _name):
            return _Query()

    monkeypatch.setattr("sector_priority.create_client", lambda _u, _k: _Client())
    monkeypatch.setattr(
        "sector_priority.fetch_nse_bulk_block_deals",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )
    monkeypatch.setattr(
        "sector_priority.fetch_nse_corporate_announcements",
        lambda *_args, **_kwargs: {"items": [], "error": "nse_empty"},
    )

    listicle_rss = """<rss><channel><title>Google News</title>
    <item><title>5 stocks to buy today including HAL</title><link>https://x/list</link>
    <pubDate>Tue, 02 Jan 2026 09:00:00 GMT</pubDate><description>Broker picks</description></item>
    </channel></rss>"""
    good_rss = """<rss><channel><title>Google News</title>
    <item><title>Hindustan Aeronautics wins export order</title><link>https://x/hal</link>
    <pubDate>Tue, 02 Jan 2026 09:00:00 GMT</pubDate><description>Order win</description></item>
    </channel></rss>"""

    def _fake_get(url, timeout_seconds=10.0):
        if "when%3A7d" in url or "when:7d" in url:
            return listicle_rss, None
        if "stock+India" in url or "Hindustan" in url:
            return good_rss, None
        return "<rss><channel><title>Google News</title></channel></rss>", None

    monkeypatch.setattr("sector_priority._http_get_text", _fake_get)
    out = fetch_stock_news_for_symbol(
        make_cfg(),
        symbol="HAL",
        exchange="NSE",
        now_utc=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
    )
    assert out["items"]
    assert out["fallback_used"] is True
    assert "export order" in out["items"][0]["title"]


def test_map_news_to_sector_scores_includes_data_centre():
    from sector_priority import map_news_to_sector_scores

    news_items = [
        {
            "title": "Hyperscale data center investment surge",
            "summary": "Cloud region capex expands and contract wins accelerate",
            "source": "TestWire",
            "url": "https://x/news1",
            "published_at": "2026-01-02T10:00:00+00:00",
        },
        {
            "title": "Missile procurement faces downgrade risk",
            "summary": "Defence budget cuts trigger uncertainty",
            "source": "TestWire",
            "url": "https://x/news2",
            "published_at": "2026-01-02T11:00:00+00:00",
        },
    ]
    scores = map_news_to_sector_scores(news_items)
    assert scores["data_centre"]["matched_items"] >= 1
    assert scores["data_centre"]["score"] > 0
    assert scores["defence"]["matched_items"] >= 1
    assert scores["defence"]["score"] < 0


def test_build_sector_rankings_news_blend_bounded(monkeypatch):
    from sector_priority import build_sector_rankings
    from sector_registry import SectorInstrument

    monkeypatch.setenv("TITAN_NEWS_BLEND_WEIGHT", "10")
    monkeypatch.setenv("TITAN_NEWS_BLEND_CAP", "1.5")
    monkeypatch.setattr("breeze_client.create_breeze_session", lambda _cfg: object())
    monkeypatch.setattr(
        "sector_priority.fetch_equity_data",
        lambda *_args, **_kwargs: pd.DataFrame({"close": [10, 11, 12, 13, 14, 15], "volume": [100] * 6}),
    )
    monkeypatch.setattr("sector_priority.fetch_nse_market_cap_inr_cr", lambda _sym: (12000.0, "nse_quote_rupees"))
    monkeypatch.setattr("sector_priority.fetch_moneycontrol_market_cap_inr_cr", lambda _sym: (None, "x"))
    monkeypatch.setattr("sector_priority.fetch_screener_market_cap_inr_cr", lambda _sym: (None, "x"))
    monkeypatch.setattr("sector_priority.fetch_yahoo_market_cap_inr_cr", lambda _s, _e: (None, "x"))
    monkeypatch.setattr("sector_priority._load_previous_market_caps", lambda cfg, sector_key: {})
    monkeypatch.setattr(
        "sector_priority.fetch_latest_global_news",
        lambda: [
            {
                "title": "AI model training investment surge",
                "summary": "Cloud capex growth",
                "source": "FeedX",
                "url": "https://x",
                "published_at": "2026-01-02T10:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        "sector_priority.map_news_to_sector_scores",
        lambda _items: {
            "ai": {
                "score": 1.0,
                "confidence": 0.9,
                "matched_items": 1,
                "drivers_top": [{"title": "AI model training investment surge", "contribution": 0.5}],
                "drivers_boosting": [{"title": "AI model training investment surge", "contribution": 0.5}],
                "drivers_dragging": [],
            }
        },
    )

    rows = build_sector_rankings(
        make_cfg(),
        sector_key="ai",
        instruments=[SectorInstrument("AAA", "NSE"), SectorInstrument("BBB", "NSE")],
        top_n=2,
    )
    assert len(rows) == 2
    for row in rows:
        meta = row["meta"]
        news = meta["news"]
        assert news["blend_points"] == 1.5
        assert round(row["rank_score"] - meta["technical_rank_score"], 4) == 1.5
        assert news["drivers_boosting"]


def test_build_sector_rankings_news_fallback_when_unavailable(monkeypatch):
    from sector_priority import build_sector_rankings
    from sector_registry import SectorInstrument

    monkeypatch.setattr("breeze_client.create_breeze_session", lambda _cfg: object())
    monkeypatch.setattr(
        "sector_priority.fetch_equity_data",
        lambda *_args, **_kwargs: pd.DataFrame({"close": [10, 11, 12, 13, 14, 15], "volume": [100] * 6}),
    )
    monkeypatch.setattr("sector_priority.fetch_nse_market_cap_inr_cr", lambda _sym: (12000.0, "nse_quote_rupees"))
    monkeypatch.setattr("sector_priority.fetch_moneycontrol_market_cap_inr_cr", lambda _sym: (None, "x"))
    monkeypatch.setattr("sector_priority.fetch_screener_market_cap_inr_cr", lambda _sym: (None, "x"))
    monkeypatch.setattr("sector_priority.fetch_yahoo_market_cap_inr_cr", lambda _s, _e: (None, "x"))
    monkeypatch.setattr("sector_priority._load_previous_market_caps", lambda cfg, sector_key: {})
    monkeypatch.setattr("sector_priority.fetch_latest_global_news", lambda: [])
    rows = build_sector_rankings(
        make_cfg(),
        sector_key="ai",
        instruments=[SectorInstrument("AAA", "NSE")],
        top_n=1,
    )
    news = rows[0]["meta"]["news"]
    assert news["blend_points"] == 0.0
    assert news["reason"] == "news_unavailable"


def test_score_sector_news_exposes_correlation_fields():
    from sector_priority import score_sector_news

    out = score_sector_news(
        [
            {
                "title": "AI chip investment surge",
                "summary": "Cloud capex expansion and growth",
                "source": "FeedX",
                "url": "https://x/news",
                "published_at": "2026-01-02T10:00:00+00:00",
            }
        ],
        sector_key="ai",
    )
    assert out["drivers_top"]
    d = out["drivers_top"][0]
    assert d["driver"] == "AI chip investment surge"
    assert d["affected_metric"] == "rank_score"
    assert d["affected_theme"] == "ai"
    assert d["direction"] in ("tailwind", "neutral", "headwind")


def test_resolve_global_news_snapshot_uses_cached_when_fresh(monkeypatch):
    from sector_priority import resolve_global_news_snapshot

    now = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    cached = {
        "refreshed_at": (now - timedelta(minutes=30)).isoformat(),
        "item_count": 1,
        "fetch_status": "ok",
        "refresh_error": "",
        "news_items": [{"title": "cached"}],
        "sector_scores": {"ai": {"score": 0.3, "matched_items": 1}},
    }
    monkeypatch.setattr("sector_priority._load_latest_news_snapshot", lambda _cfg: cached)
    monkeypatch.setattr(
        "sector_priority.refresh_global_news_snapshot",
        lambda _cfg, now_utc=None: (_ for _ in ()).throw(AssertionError("should not refresh")),
    )
    out = resolve_global_news_snapshot(make_cfg(), now_utc=now)
    assert out["source"] == "cached"
    assert out["fresh"] is True
    assert out["item_count"] == 1


def test_resolve_global_news_snapshot_stale_refresh_failure_falls_back(monkeypatch):
    from sector_priority import resolve_global_news_snapshot

    now = datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    cached = {
        "refreshed_at": (now - timedelta(hours=5)).isoformat(),
        "item_count": 2,
        "fetch_status": "ok",
        "refresh_error": "",
        "news_items": [{"title": "stale"}],
        "sector_scores": {"ai": {"score": -0.2, "matched_items": 1}},
    }
    monkeypatch.setattr("sector_priority._load_latest_news_snapshot", lambda _cfg: cached)

    def _boom(_cfg, now_utc=None, timeout_seconds=12.0):
        raise RuntimeError("feed timeout")

    monkeypatch.setattr("sector_priority.refresh_global_news_snapshot", _boom)
    out = resolve_global_news_snapshot(make_cfg(), now_utc=now)
    assert out["source"] == "stale_fallback"
    assert out["fresh"] is False
    assert "feed timeout" in (out.get("refresh_error") or "")


def test_calendar_event_gate_triggers_on_dividend():
    from sector_priority import _calendar_event_gate, _calendar_purpose_is_event

    assert _calendar_purpose_is_event("Interim Dividend - Rs 5")
    assert _calendar_purpose_is_event("Financial Results")
    assert not _calendar_purpose_is_event("Annual General Meeting")
    ctx = {
        "calendar": {
            "RELIANCE": [
                {"ex_date": "2026-06-15", "purpose": "Interim Dividend - Rs 5"},
                {"ex_date": "2026-06-20", "purpose": "Annual General Meeting"},
            ]
        },
        "calendar_window_end": "2026-06-19",
    }
    gate = _calendar_event_gate("RELIANCE", ctx)
    assert gate["gate"] == "calendar_event"
    assert gate["triggered"] is True
    assert gate["n_events"] == 1
    assert gate["withhold"] is False  # default shadow mode


def test_calendar_event_gate_noop_when_empty():
    from sector_priority import _calendar_event_gate

    gate = _calendar_event_gate("ZZZ", {"calendar": {}})
    assert gate["triggered"] is False
    assert gate["n_events"] == 0


def test_institutional_gate_damp_when_fii_negative(monkeypatch):
    from sector_priority import _institutional_gate

    monkeypatch.setenv("TITAN_INSTITUTIONAL_GATE_MODE", "damp")
    ctx = {"institutional": {"as_of_date": "2026-06-12", "fii_net_crs": -1082.0, "dii_net_crs": 500.0}}
    gate = _institutional_gate(ctx)
    assert gate["gate"] == "institutional"
    assert gate["triggered"] is True
    assert gate["score_multiplier"] == 0.85
    assert gate["withhold"] is False
    assert any("FII net" in r for r in gate["reasons"])


def test_institutional_gate_shadow_no_effect(monkeypatch):
    from sector_priority import _institutional_gate

    monkeypatch.setenv("TITAN_INSTITUTIONAL_GATE_MODE", "shadow")
    ctx = {"institutional": {"fii_net_crs": -500.0}}
    gate = _institutional_gate(ctx)
    assert gate["triggered"] is True
    assert gate["score_multiplier"] == 1.0


def test_gates_default_enforce_bumps_shadow_default(monkeypatch):
    from sector_priority import _gate_mode

    monkeypatch.delenv("TITAN_DELIVERY_GATE_MODE", raising=False)
    monkeypatch.setenv("TITAN_GATES_DEFAULT_ENFORCE", "true")
    assert _gate_mode("TITAN_DELIVERY_GATE_MODE") == "damp"


def test_rehydrate_persisted_gate_record_applies_runtime_mode(monkeypatch):
    from sector_priority import rehydrate_persisted_gate_record

    monkeypatch.setenv("TITAN_DELIVERY_GATE_MODE", "damp")
    shadow = {
        "gate": "delivery_churn",
        "mode": "shadow",
        "triggered": True,
        "would": "damp/withhold (churn)",
        "reasons": ["avg delivery 13% < floor 35%"],
        "score_multiplier": 1.0,
        "withhold": False,
    }
    refreshed = rehydrate_persisted_gate_record(shadow)
    assert refreshed["mode"] == "damp"
    assert refreshed["score_multiplier"] == 0.5
    assert refreshed["withhold"] is False


def test_rehydrate_institutional_context_applies_runtime_mode(monkeypatch):
    from sector_priority import rehydrate_institutional_context

    monkeypatch.setenv("TITAN_INSTITUTIONAL_GATE_MODE", "damp")
    ctx = {
        "risk_off": True,
        "mode": "shadow",
        "fii_net_crs": -1082.0,
        "dii_net_crs": 5341.0,
        "gate_applied": False,
    }
    refreshed = rehydrate_institutional_context(ctx)
    assert refreshed["mode"] == "damp"
    assert refreshed["gate_applied"] is True


def test_pledge_slb_gate_off_by_default(monkeypatch):
    from sector_priority import _pledge_slb_gate

    monkeypatch.delenv("TITAN_PLEDGE_SLB_GATE_MODE", raising=False)
    assert _pledge_slb_gate("RELIANCE", {}) is None


def test_pledge_slb_gate_shadow_logs_not_implemented(monkeypatch):
    from sector_priority import _pledge_slb_gate

    monkeypatch.setenv("TITAN_PLEDGE_SLB_GATE_MODE", "shadow")
    gate = _pledge_slb_gate("RELIANCE", {})
    assert gate is not None
    assert gate["status"] == "not_implemented"
    assert gate["triggered"] is False


def test_breeze_data_freshness_gate_shadow_when_stale(monkeypatch):
    from sector_priority import _breeze_data_freshness_gate

    monkeypatch.setattr("breeze_client.is_breeze_data_stale", lambda: True)
    monkeypatch.setattr("breeze_client.breeze_data_stale_reason", lambda: "token missing")
    monkeypatch.setattr("breeze_client.breeze_stale_hard_stop_enabled", lambda: False)
    monkeypatch.setenv("TITAN_BREEZE_FRESHNESS_GATE_MODE", "shadow")
    gate = _breeze_data_freshness_gate()
    assert gate["triggered"] is True
    assert gate["withhold"] is False
    assert gate["would"] == "withhold (stale Breeze data)"


def test_breeze_data_freshness_gate_hard_stop_skip(monkeypatch):
    from sector_priority import _breeze_data_freshness_gate

    monkeypatch.setattr("breeze_client.is_breeze_data_stale", lambda: True)
    monkeypatch.setattr("breeze_client.breeze_data_stale_reason", lambda: "expired")
    monkeypatch.setattr("breeze_client.breeze_stale_hard_stop_enabled", lambda: True)
    monkeypatch.setenv("TITAN_BREEZE_FRESHNESS_GATE_MODE", "skip")
    gate = _breeze_data_freshness_gate()
    assert gate["triggered"] is True
    assert gate["withhold"] is True


def test_live_regime_read_off_by_default(monkeypatch):
    from sector_priority import _live_regime_read_enabled

    monkeypatch.delenv("TITAN_LIVE_REGIME_READ", raising=False)
    assert _live_regime_read_enabled() is False


def test_merge_regime_with_live_snapshot_shadow_only():
    from sector_priority import _merge_regime_with_live_snapshot, _regime_gate_decision

    eod = _regime_gate_decision([])
    live = {
        "index_code": "NIFTY BANK",
        "snapshot_ts": "2026-06-14T10:00:00+05:30",
        "regime_state": "risk_off",
        "pct_vs_prev_close": -0.5,
        "pct_vs_open": -0.2,
    }
    merged = _merge_regime_with_live_snapshot(eod, live, sector_key="banks_psu")
    assert merged["triggered"] is False
    assert merged["withhold"] is False
    assert merged["live_would_trigger"] is True
    assert merged["live_regime_state"] == "risk_off"
    assert any("shadow" in r for r in merged.get("reasons", []))


def test_merge_regime_with_live_snapshot_fallback_to_eod():
    from sector_priority import _merge_regime_with_live_snapshot, _regime_gate_decision

    eod = _regime_gate_decision(
        [
            {"trade_date": "2026-06-01", "breadth_above_ema200_pct": 30.0, "avg_effective_intent_score": 50.0},
            {"trade_date": "2026-06-02", "breadth_above_ema200_pct": 25.0, "avg_effective_intent_score": 45.0},
        ]
    )
    merged = _merge_regime_with_live_snapshot(eod, None, sector_key="defence")
    assert merged == eod


def test_sector_benchmark_index_code_maps_psu_banks():
    from sector_priority import _sector_benchmark_index_code

    assert _sector_benchmark_index_code("banks_psu") == "NIFTY BANK"
    assert _sector_benchmark_index_code("defence") == "NIFTY"


def test_v2_rank_adjustment_bonus_for_low_risk():
    from sector_priority import _v2_rank_adjustment

    out = _v2_rank_adjustment({"label": "hold", "risk_net": 3.0})
    assert out["mode"] == "bonus"
    assert out["adjustment"] > 0.0


def test_v2_rank_adjustment_penalty_for_trim():
    from sector_priority import _v2_rank_adjustment

    out = _v2_rank_adjustment({"label": "trim", "risk_net": 5.5})
    assert out["mode"] == "penalty"
    assert out["adjustment"] < 0.0


def test_v2_risk_gate_does_not_double_penalize_score(monkeypatch):
    from sector_priority import _v2_risk_gate

    monkeypatch.setenv("TITAN_V2_RISK_GATE_MODE", "damp")
    gate = _v2_risk_gate(
        "BDL",
        {"v2_labels": {"BDL": "trim"}, "v2_risk_net": {"BDL": 5.5}, "v2_label_dates": {"BDL": "2026-06-12"}},
    )
    assert gate["triggered"] is True
    assert gate["score_multiplier"] == 1.0
    assert gate["withhold"] is False
    assert gate["scoring_reconciled"] is True


def test_v2_risk_gate_withholds_extreme_exit_risk_in_skip_mode(monkeypatch):
    from sector_priority import _v2_risk_gate

    monkeypatch.setenv("TITAN_V2_RISK_GATE_MODE", "skip")
    gate = _v2_risk_gate(
        "XYZ",
        {"v2_labels": {"XYZ": "exit-risk"}, "v2_risk_net": {"XYZ": 8.0}, "v2_label_dates": {"XYZ": "2026-06-12"}},
    )
    assert gate["withhold"] is True
    assert gate["score_multiplier"] == 1.0


def test_resolve_v2_signal_follows_symbol_alias():
    from sector_priority import _resolve_v2_signal

    ctx = {
        "v2_labels": {"ETERNAL": "hold"},
        "v2_risk_net": {"ETERNAL": 3.0},
        "v2_label_dates": {"ETERNAL": "2026-06-16"},
    }
    out = _resolve_v2_signal("ZOMATO", ctx)
    assert out["label"] == "hold"
    assert out["alias_used"] == "ETERNAL"


def test_overextension_confirmation_allows_mild_weekly_pullback():
    from sector_priority import _overextension_confirmation

    mult, _reason = _overextension_confirmation(ret_1w=-1.5, ret_1m=12.0, regime_hostile=False)
    assert mult < 1.0
    assert mult > 0.2


def test_score_from_features_uses_return_percentiles():
    from sector_priority import _score_from_features

    raw = _score_from_features(
        bucket="small",
        ret_1w=20.0,
        ret_1m=10.0,
        absorption=1.0,
        percentile_1w=100.0,
        percentile_1m=100.0,
    )
    median = _score_from_features(
        bucket="small",
        ret_1w=20.0,
        ret_1m=10.0,
        absorption=1.0,
        percentile_1w=50.0,
        percentile_1m=50.0,
    )
    assert raw > median


def test_cohort_return_percentiles_defaults_nan_to_median():
    from sector_priority import _cohort_return_percentiles

    pending = [{"ret_1w": 10.0, "ret_1m": float("nan")}, {"ret_1w": 5.0, "ret_1m": 8.0}]
    _cohort_return_percentiles(pending)
    assert pending[0]["percentile_1w"] == 100.0
    assert pending[1]["percentile_1w"] == 0.0
    assert pending[0]["percentile_1m"] == 50.0
    assert pending[1]["percentile_1m"] == 50.0  # single valid cohort value -> mid-rank


def test_v2_rank_adjustment_damps_when_overextension_penalized():
    from sector_priority import _v2_rank_adjustment

    plain = _v2_rank_adjustment({"label": "trim", "risk_net": 6.0}, overextension_penalty=0.0)
    damped = _v2_rank_adjustment({"label": "trim", "risk_net": 6.0}, overextension_penalty=2.0)
    assert damped["adjustment"] > plain["adjustment"]
    assert damped["mode"] == "penalty_vol_damped"


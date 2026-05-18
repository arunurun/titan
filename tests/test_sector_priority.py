from types import SimpleNamespace

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
        def __init__(self, payload):
            self.payload = payload

        def table(self, _name):
            return _FakeQuery(self.payload)

    monkeypatch.setattr(
        "sector_priority.create_client",
        lambda _u, _k: _FakeClient(
            [
                {"symbol": "A", "exchange": "NSE", "rank_in_sector": 1},
                {"symbol": "B", "exchange": "BSE", "rank_in_sector": 2},
            ]
        ),
    )
    out = load_priority_instruments(make_cfg(), sector_key="ai", top_n=2)
    assert [f"{x.symbol}:{x.exchange}" for x in out] == ["A:NSE", "B:BSE"]


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


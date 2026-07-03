"""ICICI scrip master -> Breeze stock_code resolution."""

import pytest

from breeze_scrip_master import clear_scrip_cache_for_tests, resolve_breeze_stock_code


@pytest.fixture(autouse=True)
def _clear_scrip_cache():
    clear_scrip_cache_for_tests()
    yield
    clear_scrip_cache_for_tests()


SAMPLE_CSV = (
    "SC,SN,EC,SM,SG,TK,LS,CD,NS,TS,ISIN,SR,SI\n"
    "BHAELE,BHARAT ELECTRONICS LTD,NSE,BHAELE,EQUITY,383,1,BHAELE,BEL,0.05,INE263A01024,EQ,\n"
    "NIFTY,NIFTY 50,NSE,NIFTY,INDEX,26000,1,NIFTY,NIFTY,0,NIFTY,,\n"
    "NAVFLU,NAVIN FLUORINE INTERNATIONAL L,NSE,NAVFLU,EQUITY,14672,1,NAVFLU,NAVINFLUOR,0.5,INE048G01026,EQ,\n"
)


def test_resolve_bel_to_bhaele(monkeypatch, tmp_path):
    monkeypatch.setattr("breeze_scrip_master._fetch_scrip_csv", lambda p: SAMPLE_CSV)
    monkeypatch.setattr("breeze_scrip_master._default_cache_path", lambda: tmp_path / "StockScriptNew.csv")

    assert resolve_breeze_stock_code("BEL", "NSE") == "BHAELE"


def test_resolve_nifty_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr("breeze_scrip_master._fetch_scrip_csv", lambda p: SAMPLE_CSV)
    monkeypatch.setattr("breeze_scrip_master._default_cache_path", lambda: tmp_path / "StockScriptNew.csv")

    assert resolve_breeze_stock_code("NIFTY", "NSE") == "NIFTY"


def test_resolve_unknown_falls_back_to_symbol(monkeypatch, tmp_path):
    monkeypatch.setattr("breeze_scrip_master._fetch_scrip_csv", lambda p: SAMPLE_CSV)
    monkeypatch.setattr("breeze_scrip_master._default_cache_path", lambda: tmp_path / "StockScriptNew.csv")

    assert resolve_breeze_stock_code("ZZZZUNKNOWN", "NSE") == "ZZZZUNKNOWN"


def test_resolve_navinfluor_to_navflu(monkeypatch, tmp_path):
    monkeypatch.setattr("breeze_scrip_master._fetch_scrip_csv", lambda p: SAMPLE_CSV)
    monkeypatch.setattr("breeze_scrip_master._default_cache_path", lambda: tmp_path / "StockScriptNew.csv")

    assert resolve_breeze_stock_code("NAVINFLUOR", "NSE") == "NAVFLU"


def test_fetch_failure_falls_back(monkeypatch):
    def boom(_p):
        raise OSError("network")

    monkeypatch.setattr("breeze_scrip_master._fetch_scrip_csv", boom)

    assert resolve_breeze_stock_code("BEL", "NSE") == "BHAELE"
    assert resolve_breeze_stock_code("INDUSTOWER", "NSE") == "BHAINF"

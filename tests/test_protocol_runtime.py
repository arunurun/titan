from datetime import datetime

from protocol_runtime import (
    cluster_instruments,
    resolve_protocol_runs,
    should_run_window_now,
)


def test_should_run_open_window_true_within_tolerance():
    now = datetime(2026, 4, 13, 9, 17)  # Monday
    assert should_run_window_now("open", now_ist=now, tolerance_minutes=3)


def test_should_run_cluster0_every_30_minutes():
    ok = datetime(2026, 4, 13, 10, 30)  # Monday
    bad = datetime(2026, 4, 13, 10, 25)
    assert should_run_window_now("cluster0", now_ist=ok)
    assert not should_run_window_now("cluster0", now_ist=bad)


def test_cluster_instruments_returns_nse_symbols():
    inst = cluster_instruments("clusterA")
    assert inst
    assert all(x.exchange == "NSE" for x in inst)


def test_resolve_protocol_runs_strict_filters_non_due_window():
    now = datetime(2026, 4, 13, 10, 2)  # Monday
    runs = resolve_protocol_runs(window="open", now_ist=now, strict_window=True)
    assert runs == []


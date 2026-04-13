from datetime import datetime
from pathlib import Path

from protocol_loop import build_protocol_command, due_windows, window_slot_key


def test_window_slot_key_open_once_per_day():
    now = datetime(2026, 4, 13, 9, 15)
    assert window_slot_key("open", now) == "2026-04-13:open"


def test_window_slot_key_cluster0_30min_bucket():
    now = datetime(2026, 4, 13, 10, 44)
    assert window_slot_key("cluster0", now) == "2026-04-13:cluster0:10:30"


def test_due_windows_exact_time():
    now = datetime(2026, 4, 13, 11, 30)
    assert due_windows(now_ist=now, windows=("mid",), open_mid_tolerance_minutes=0) == ["mid"]


def test_build_protocol_command_contains_expected_flags():
    cmd = build_protocol_command(
        python_exe="python",
        root=Path("C:/proj"),
        window="cluster0",
        clusters_csv="cluster0",
        macro_json="macro.json",
        events_json="events.json",
        sector_workers=4,
        sector_max_symbols=3,
    )
    s = " ".join(cmd)
    assert "--protocol-run" in s
    assert "--protocol-window cluster0" in s
    assert "--strict-window" in s
    assert "--protocol-clusters cluster0" in s
    assert "--macro-json macro.json" in s
    assert "--events-json events.json" in s
    assert "--sector-workers 4" in s
    assert "--sector-max-symbols 3" in s


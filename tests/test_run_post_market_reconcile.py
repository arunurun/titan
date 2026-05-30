from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "run_post_market_reconcile.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("run_post_market_reconcile", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_live_reconcile_sets_report_only_env(monkeypatch):
    mod = _load_script_module()
    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0

    def _fake_run(cmd, cwd, env, check):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        captured["check"] = check
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    rc = mod._run_live_reconcile(
        sector=None,
        all_stocks=True,
        max_symbols=8,
        workers=2,
    )
    assert rc == 0
    assert "--all-sectors" in captured["cmd"]
    assert captured["env"]["TITAN_ENABLE_ANALYSIS_STORE"] == "1"
    assert captured["env"]["TITAN_RECONCILE_REPORT_ONLY"] == "1"
    assert captured["env"]["TITAN_ALL_SECTORS_SINGLE_DIGEST"] == "1"

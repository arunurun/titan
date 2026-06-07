"""Regenerate config/fno_breeze_mapping.yaml from NSE fo_mktlots and ICICI scrip master."""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
INDEX_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})


def _download_fo_mktlots() -> str:
    req = urllib.request.Request(
        FO_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    cache_dir = ROOT / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "fo_mktlots.csv").write_bytes(data)
    return data.decode("utf-8", errors="replace")


def _parse_fo_symbols(text: str) -> set[str]:
    out: set[str] = set()
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [f.strip() for f in reader.fieldnames]
    for row in reader:
        sym = (row.get("SYMBOL") or "").strip().upper()
        if sym and sym not in INDEX_UNDERLYINGS:
            out.add(sym)
    return out


def _sector_symbols() -> set[str]:
    syms: set[str] = set()
    for path in (ROOT / "data" / "sector_allowlists").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for s in data.get("symbols", []):
            syms.add(str(s).strip().upper())
    return syms


def _fno_yaml_symbols() -> set[str]:
    symbols: set[str] = set()
    text = (ROOT / "config" / "fno_symbols.yaml").read_text(encoding="utf-8")
    in_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("symbols:"):
            in_list = True
            continue
        if in_list and stripped.startswith("- "):
            sym = stripped[2:].strip().strip('"').strip("'").upper()
            if sym:
                symbols.add(sym)
        elif in_list:
            in_list = False
    return symbols


def _scrip_lookup() -> dict[str, str]:
    path = ROOT / "data" / "cache" / "StockScriptNew.csv"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} missing — run a Breeze scrip fetch first or copy StockScriptNew.csv into data/cache/"
        )
    out: dict[str, str] = {}
    for row in csv.DictReader(path.open(encoding="utf-8", errors="replace")):
        ec = (row.get("EC") or "").strip().upper()
        ns = (row.get("NS") or "").strip().upper()
        sc = (row.get("SC") or "").strip().upper()
        if ec == "NSE" and ns and sc:
            out.setdefault(ns, sc)
    return out


def _static_aliases() -> dict[str, str]:
    path = ROOT / "data" / "breeze_nse_aliases.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(k).strip().upper(): str(v).strip().upper() for k, v in raw.items()}


def build_mapping() -> dict[str, str]:
    fo_text = _download_fo_mktlots()
    fo_syms = _parse_fo_symbols(fo_text)
    fno_set = _fno_yaml_symbols()
    sector_fno = _sector_symbols() & fo_syms

    bad_fno = sorted(fno_set - fo_syms)
    missing = sorted(sector_fno - fno_set)
    if bad_fno:
        print(f"WARNING: fno_symbols.yaml entries not in fo_mktlots: {bad_fno}", file=sys.stderr)
    if missing:
        print(f"WARNING: sector F&O missing from fno_symbols.yaml: {missing}", file=sys.stderr)

    lookup = _scrip_lookup()
    aliases = _static_aliases()
    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for sym in sorted(fno_set):
        code = lookup.get(sym) or aliases.get(sym)
        if not code:
            code = sym
            if sym not in lookup and sym not in aliases:
                unresolved.append(sym)
        mapping[sym] = code

    if unresolved:
        print(f"WARNING: no scrip/alias for: {unresolved}", file=sys.stderr)

    print(f"fo_mktlots stock underlyings: {len(fo_syms)}")
    print(f"sector F&O: {len(sector_fno)}")
    print(f"mapping entries: {len(mapping)}")
    return mapping


def write_mapping_yaml(mapping: dict[str, str]) -> Path:
    lines = [
        "# NSE display symbol -> Breeze NFO stock_code for option chain API.",
        "# Validated against NSE fo_mktlots.csv and ICICI StockScriptNew.csv.",
        "# Regenerate: python scripts/build_fno_breeze_mapping.py",
        "#",
        "mapping:",
    ]
    for sym in sorted(mapping):
        lines.append(f"  {sym}: {mapping[sym]}")
    out = ROOT / "config" / "fno_breeze_mapping.yaml"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> int:
    mapping = build_mapping()
    out = write_mapping_yaml(mapping)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

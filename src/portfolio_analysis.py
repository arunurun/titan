"""Portfolio holdings parsing and analysis helpers for control UI."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_loader import load_config
from sector_audit import build_equity_live_audit
from sector_registry import SectorInstrument

_EXCHANGES = {"NSE", "BSE"}
_LINE_SPLIT_RE = re.compile(r"[,\t|;]+")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9&.\-]{0,19}$")
_QTY_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_COMMON_NON_SYMBOLS = {
    "HOLDINGS",
    "HOLDING",
    "SYMBOL",
    "COMPANY",
    "QUANTITY",
    "QTY",
    "PRICE",
    "VALUE",
    "TOTAL",
    "AVG",
    "AVERAGE",
    "COST",
    "INVESTED",
    "UNITS",
    "UNIT",
    "ISIN",
    "PORTFOLIO",
}


@dataclass(frozen=True)
class PortfolioHolding:
    symbol: str
    exchange: str
    quantity: float
    source_line: str


def _try_load_pdf_reader():
    try:
        from pypdf import PdfReader  # type: ignore

        return PdfReader, None
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore

            return PdfReader, None
        except Exception as exc:
            return None, (
                "PDF parsing dependency is unavailable (install pypdf or PyPDF2). "
                "Use pasted holdings text instead."
            )


def extract_text_from_pdf(path_raw: str) -> tuple[str, str | None]:
    path = Path(path_raw).expanduser()
    if not path.is_file():
        raise ValueError(f"PDF file not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {path.name}")

    reader_cls, limitation = _try_load_pdf_reader()
    if reader_cls is None:
        return "", limitation

    try:
        reader = reader_cls(str(path))
        text_parts: list[str] = []
        for page in getattr(reader, "pages", []):
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                continue
    except Exception as exc:
        return "", f"Unable to parse PDF ({exc}). Use pasted holdings text as fallback."

    text = "\n".join(text_parts).strip()
    if not text:
        return "", "PDF parsed but no extractable text was found. Use pasted holdings text."
    return text, None


def _normalize_symbol(symbol_raw: str) -> str:
    out = "".join(ch for ch in symbol_raw.upper().strip() if ch.isalnum() or ch in {"&", ".", "-"})
    return out


def _parse_symbol_with_exchange(token: str) -> tuple[str, str] | None:
    raw = token.strip().upper()
    if not raw:
        return None
    for sep in (":", "-", "/"):
        if sep in raw:
            left, right = [x.strip() for x in raw.split(sep, 1)]
            if left in _EXCHANGES and _SYMBOL_RE.match(_normalize_symbol(right)):
                return left, _normalize_symbol(right)
            if right in _EXCHANGES and _SYMBOL_RE.match(_normalize_symbol(left)):
                return right, _normalize_symbol(left)
    sym = _normalize_symbol(raw)
    if _SYMBOL_RE.match(sym):
        return "NSE", sym
    return None


def _parse_line(line: str) -> PortfolioHolding | None:
    cleaned = line.strip()
    if not cleaned:
        return None
    if cleaned.startswith("#"):
        return None

    tokens = [t.strip() for t in _LINE_SPLIT_RE.split(cleaned) if t.strip()]
    if len(tokens) == 1:
        tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if len(tokens) < 2:
        return None

    qty_idx = -1
    qty = float("nan")
    for i, token in enumerate(tokens):
        tok = token.replace(",", "")
        if _QTY_RE.match(tok):
            qty_idx = i
            qty = float(tok)
            break
    if qty_idx < 0 or math.isnan(qty) or qty == 0.0:
        return None

    symbol_token = ""
    exchange = "NSE"
    symbol = ""
    for j in range(qty_idx - 1, -1, -1):
        parsed = _parse_symbol_with_exchange(tokens[j])
        if not parsed:
            continue
        exch, sym = parsed
        if sym in _COMMON_NON_SYMBOLS:
            continue
        exchange = exch
        symbol = sym
        symbol_token = tokens[j]
        break
    if not symbol:
        return None

    # Reject obvious header lines like "SYMBOL QTY".
    if symbol in _COMMON_NON_SYMBOLS or symbol_token.upper() in _COMMON_NON_SYMBOLS:
        return None
    return PortfolioHolding(symbol=symbol, exchange=exchange, quantity=qty, source_line=cleaned)


def parse_holdings_text(text: str) -> list[PortfolioHolding]:
    grouped: dict[tuple[str, str], float] = {}
    for raw_line in (text or "").splitlines():
        parsed = _parse_line(raw_line)
        if not parsed:
            continue
        key = (parsed.symbol, parsed.exchange)
        grouped[key] = grouped.get(key, 0.0) + parsed.quantity
    out = [
        PortfolioHolding(symbol=symbol, exchange=exchange, quantity=round(quantity, 4), source_line="")
        for (symbol, exchange), quantity in grouped.items()
        if quantity != 0.0
    ]
    out.sort(key=lambda x: (x.exchange, x.symbol))
    return out


def collect_holdings_input(
    *,
    pdf_path: str,
    pasted_holdings_text: str,
) -> tuple[list[PortfolioHolding], str, list[str]]:
    limitations: list[str] = []
    holdings: list[PortfolioHolding] = []
    source = "pasted_text"

    if (pdf_path or "").strip():
        pdf_text, limitation = extract_text_from_pdf(pdf_path)
        if limitation:
            limitations.append(limitation)
        if pdf_text:
            holdings = parse_holdings_text(pdf_text)
            source = "pdf"
            if not holdings:
                limitations.append("PDF text was read but no holdings pattern matched; fallback text was used.")

    if not holdings and (pasted_holdings_text or "").strip():
        holdings = parse_holdings_text(pasted_holdings_text)
        source = "pasted_text"

    if not holdings:
        limitations.append(
            "No holdings parsed. Use lines like `NSE:RELIANCE, 10` or `INFY 5` in pasted fallback text."
        )
    return holdings, source, limitations


def analyze_portfolio_holdings(
    holdings: list[PortfolioHolding],
    *,
    max_positions: int = 20,
) -> dict[str, Any]:
    capped = holdings[: max(1, int(max_positions))]
    cfg = load_config()
    from breeze_client import create_breeze_session

    breeze = create_breeze_session(cfg)
    rows: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted_next_week = 0.0
    weighted_intent = 0.0

    for h in capped:
        try:
            audit, _ = build_equity_live_audit(
                cfg,
                breeze,
                SectorInstrument(symbol=h.symbol, exchange=h.exchange),
                sector_id="portfolio_custom",
                with_narrative=False,
                strict_data=False,
            )
            if audit.get("skipped_no_data"):
                rows.append(
                    {
                        "symbol": h.symbol,
                        "exchange": h.exchange,
                        "quantity": h.quantity,
                        "status": "skipped_no_data",
                    }
                )
                continue
            next_week = audit.get("next_week_score")
            intent = audit.get("effective_intent_score", audit.get("intent_score"))
            weight = abs(float(h.quantity))
            total_weight += weight
            if isinstance(next_week, (int, float)):
                weighted_next_week += float(next_week) * weight
            if isinstance(intent, (int, float)):
                weighted_intent += float(intent) * weight
            rows.append(
                {
                    "symbol": h.symbol,
                    "exchange": h.exchange,
                    "quantity": h.quantity,
                    "status": "ok",
                    "next_week_score": next_week,
                    "intent_score": intent,
                    "z_score": audit.get("z_score"),
                    "return_1d_pct": audit.get("return_1d_pct"),
                    "absorption_ratio": audit.get("absorption_ratio"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "symbol": h.symbol,
                    "exchange": h.exchange,
                    "quantity": h.quantity,
                    "status": "error",
                    "error": str(exc),
                }
            )

    ok_rows = [r for r in rows if r.get("status") == "ok"]
    skipped_rows = [r for r in rows if r.get("status") == "skipped_no_data"]
    err_rows = [r for r in rows if r.get("status") == "error"]
    summary = {
        "requested_positions": len(capped),
        "analyzed_positions": len(ok_rows),
        "skipped_no_data": len(skipped_rows),
        "errors": len(err_rows),
        "portfolio_weighted_next_week_score": round(weighted_next_week / total_weight, 2)
        if total_weight > 0
        else None,
        "portfolio_weighted_intent_score": round(weighted_intent / total_weight, 2)
        if total_weight > 0
        else None,
    }
    ranked = sorted(
        ok_rows,
        key=lambda x: float(x.get("next_week_score")) if isinstance(x.get("next_week_score"), (int, float)) else -999.0,
        reverse=True,
    )
    return {
        "summary": summary,
        "top_candidates": ranked[:5],
        "rows": rows,
    }


def portfolio_report_text(
    *,
    source: str,
    limitations: list[str],
    parsed_count: int,
    result: dict[str, Any],
) -> str:
    payload = {
        "source": source,
        "parsed_holdings": parsed_count,
        "limitations": limitations,
        **result,
    }
    return json.dumps(payload, indent=2)

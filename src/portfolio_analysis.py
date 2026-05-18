"""Portfolio holdings parsing and analysis helpers for control UI."""

from __future__ import annotations

import json
import difflib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_loader import load_config
from supabase import create_client
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
    "SUBTOTAL",
    "AVG",
    "AVERAGE",
    "COST",
    "INVESTED",
    "UNITS",
    "UNIT",
    "ISIN",
    "PORTFOLIO",
}
_SYMBOL_ALIAS_HINTS = {
    # Common PDF truncations/contract-like tails mapped to equities.
    "ASTMIC": "ASTRAMICRO",
    "DATPAT": "DATAPATTNS",
    "JYORES": "JYOTIRES",
    # PDF / contract codes (ICICI Breeze stock_code ≠ NSE display symbol).
    # BHAELE is BEL (Bharat Electronics); do not confuse with BHEL (Bharat Heavy Electricals).
    "BHAELE": "BEL",
    "BHAEL": "BEL",
    # Statement contract codes (vary by broker naming).
    "HBLPOW": "HBLENGINE",
    "DSPGOL": "GOLDETFADD",
    "SBIGOL": "SETFGOLD",
    "SBISIL": "SBISILVER",
    "SOLIN": "SOLARINDS",
}


@dataclass(frozen=True)
class PortfolioHolding:
    symbol: str
    exchange: str
    quantity: float
    source_line: str
    avg_buy_price: float | None = None


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


def _clean_symbol_key(symbol_raw: str) -> str:
    return "".join(ch for ch in str(symbol_raw or "").upper() if ch.isalnum())


def _strip_numeric_suffix(symbol_key: str) -> str:
    key = _clean_symbol_key(symbol_key)
    stripped = re.sub(r"\d{2,}$", "", key)
    return stripped if len(stripped) >= 3 else key


def _load_supabase_breeze_token(cfg) -> str | None:
    try:
        client = create_client(cfg.supabase_url, cfg.supabase_key)
        res = (
            client.table("session_config")
            .select("breeze_session_token")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if rows:
            tok = (rows[0].get("breeze_session_token") or "").strip()
            if tok:
                return tok
    except Exception:
        return None
    return None


def _load_active_symbol_universe(cfg) -> dict[str, dict[str, str]]:
    by_exchange: dict[str, dict[str, str]] = {"NSE": {}, "BSE": {}}
    try:
        client = create_client(cfg.supabase_url, cfg.supabase_key)
        page = 0
        size = 1000
        while True:
            res = (
                client.table("market_instruments")
                .select("symbol,exchange,is_active")
                .eq("is_active", True)
                .range(page * size, page * size + size - 1)
                .execute()
            )
            rows = list(getattr(res, "data", None) or [])
            if not rows:
                break
            for row in rows:
                sym = str(row.get("symbol") or "").strip().upper()
                ex = str(row.get("exchange") or "").strip().upper()
                if not sym or ex not in by_exchange:
                    continue
                by_exchange[ex][_clean_symbol_key(sym)] = sym
            if len(rows) < size:
                break
            page += 1
    except Exception:
        return by_exchange
    return by_exchange


def _resolve_symbol(
    symbol: str,
    exchange: str,
    *,
    by_exchange: dict[str, dict[str, str]],
) -> tuple[str, str, str, float]:
    ex = exchange if exchange in _EXCHANGES else "NSE"
    key_raw = _clean_symbol_key(symbol)
    key_trim = _strip_numeric_suffix(key_raw)
    same_map = by_exchange.get(ex, {})
    other_ex = "BSE" if ex == "NSE" else "NSE"
    other_map = by_exchange.get(other_ex, {})

    # 1) Exact in same exchange.
    if key_raw in same_map:
        return same_map[key_raw], ex, "exact_match", 1.0

    # 2) Trimmed numeric suffix exact.
    if key_trim in same_map:
        return same_map[key_trim], ex, "numeric_suffix_stripped", 0.95

    # 3) Alias hints from known broker/PDF patterns.
    hint = _SYMBOL_ALIAS_HINTS.get(key_trim) or _SYMBOL_ALIAS_HINTS.get(key_raw)
    if hint:
        hint_key = _clean_symbol_key(hint)
        if hint_key in same_map:
            return same_map[hint_key], ex, "alias_hint", 0.93
        if hint_key in other_map:
            return other_map[hint_key], other_ex, "alias_hint_cross_exchange", 0.9

    # 4) Prefix match in same exchange (avoid SOLIN/PARDEF collapsing to a tiny key like "S").
    candidates = [
        k
        for k in same_map.keys()
        if k.startswith(key_trim)
        or (
            len(k) >= 3
            and key_trim.startswith(k)
            and len(key_trim) <= len(k) + 2
        )
    ]
    if candidates:
        if len(key_trim) >= 5:
            candidates = [c for c in candidates if len(c) >= 4]
        elif len(key_trim) >= 4:
            candidates = [c for c in candidates if len(c) >= 3]
    if candidates:
        best = min(candidates, key=lambda x: abs(len(x) - len(key_trim)))
        return same_map[best], ex, "prefix_match", 0.86

    # 5) Fuzzy match in same exchange.
    close = difflib.get_close_matches(key_trim, list(same_map.keys()), n=1, cutoff=0.72)
    if close:
        k = close[0]
        return same_map[k], ex, "fuzzy_match", 0.8

    # 6) Cross-exchange exact/trim/fuzzy fallback.
    if key_raw in other_map:
        return other_map[key_raw], other_ex, "cross_exchange_exact", 0.78
    if key_trim in other_map:
        return other_map[key_trim], other_ex, "cross_exchange_numeric_trim", 0.76
    close2 = difflib.get_close_matches(key_trim, list(other_map.keys()), n=1, cutoff=0.75)
    if close2:
        k = close2[0]
        return other_map[k], other_ex, "cross_exchange_fuzzy", 0.72

    return _normalize_symbol(symbol), ex, "unresolved", 0.0


_ROLLUP_MAX_INVESTED_SINGLE_LINE_INR = 1_000_000_000_000.0  # ₹1e12 single-line notionals treated as corrupt


def _holding_is_statement_line(h: PortfolioHolding) -> bool:
    """Reject broker PDF totals / footers mistaken for tickers."""
    k = _clean_symbol_key(h.symbol)
    raw_u = str(h.symbol or "").upper().strip()
    if len(k) >= 10 and k.startswith("TOTAL"):
        return True
    if len(k) >= 8 and k.startswith("TOTAL") and any(ch.isdigit() for ch in raw_u):
        return True
    if k in {"SUBTOTAL", "GRANDTOTAL", "TOTALVALUE", "NETVALUE", "NETASSET"}:
        return True
    return False


def _rollup_includes_position(invested: float | None, current: float | None) -> bool:
    """Exclude obvious quantity/price OCR mistakes from headline portfolio P&L."""
    if not isinstance(invested, (int, float)) or not isinstance(current, (int, float)):
        return False
    inv = float(invested)
    cur = float(current)
    if inv <= 0 or cur < 0 or math.isnan(inv) or math.isnan(cur):
        return False
    if inv > _ROLLUP_MAX_INVESTED_SINGLE_LINE_INR:
        return False
    if inv > max(500_000.0, cur * 1_000.0):
        return False
    if cur > max(500_000.0, inv * 5_000.0):
        return False
    return True


def _cost_basis_unreliable(
    *,
    avg_buy: float | None,
    current_price: float | None,
    pnl_pct: float | None,
) -> bool:
    """
    Heuristic for PDF column swaps (qty mistaken for average buy) or mis-scaled averages.
    Such rows are excluded from headline portfolio totals; digest shows n/a instead of absurd %.
    """
    if isinstance(pnl_pct, (int, float)) and not math.isnan(float(pnl_pct)) and abs(float(pnl_pct)) > 380:
        return True
    if not isinstance(avg_buy, (int, float)) or not isinstance(current_price, (int, float)):
        return False
    ab = float(avg_buy)
    cp = float(current_price)
    if ab <= 0 or cp <= 0 or math.isnan(ab) or math.isnan(cp):
        return False
    ratio = cp / ab
    if cp > 40 and ab < cp * 0.06:
        return True
    if ratio > 92:
        return True
    if ab < 3.0 and cp > 250:
        return True
    return False


def _merge_holdings_resolving_same_ticker(
    holdings: list[PortfolioHolding],
    *,
    by_exchange: dict[str, dict[str, str]],
) -> list[PortfolioHolding]:
    """Collapse multiple input codes that resolve to one listed symbol (VWAP average buy)."""
    from collections import OrderedDict

    statement_rows = [h for h in holdings if _holding_is_statement_line(h)]
    trade_like = [h for h in holdings if not _holding_is_statement_line(h)]
    bucket: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

    def _consume(h: PortfolioHolding) -> None:
        rs, rex, reason, _ = _resolve_symbol(h.symbol, h.exchange, by_exchange=by_exchange)
        if reason == "unresolved":
            key: tuple[Any, ...] = ("__unresolved__", h.symbol.upper(), h.exchange.upper())
        else:
            key = ("__resolved__", rex.upper(), rs.upper())
        if key not in bucket:
            bucket[key] = {"qty": 0.0, "cost": 0.0, "qty_cost": 0.0, "rep": h}
        b = bucket[key]
        q = float(h.quantity)
        b["qty"] += q
        if isinstance(h.avg_buy_price, (int, float)) and float(h.avg_buy_price) > 0:
            qa = abs(q)
            b["cost"] += qa * float(h.avg_buy_price)
            b["qty_cost"] += qa

    for h in trade_like:
        _consume(h)

    merged_trades = [
        PortfolioHolding(
            symbol=bucket[k]["rep"].symbol,
            exchange=bucket[k]["rep"].exchange,
            quantity=round(bucket[k]["qty"], 4),
            avg_buy_price=(
                round(bucket[k]["cost"] / bucket[k]["qty_cost"], 4) if bucket[k]["qty_cost"] > 0 else None
            ),
            source_line=bucket[k]["rep"].source_line,
        )
        for k in bucket
    ]
    return statement_rows + merged_trades


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
    avg_buy_price: float | None = None
    for k in range(qty_idx + 1, len(tokens)):
        tok = tokens[k].replace(",", "")
        if _QTY_RE.match(tok):
            cand = float(tok)
            if cand > 0:
                avg_buy_price = cand
                break
    return PortfolioHolding(
        symbol=symbol,
        exchange=exchange,
        quantity=qty,
        avg_buy_price=avg_buy_price,
        source_line=cleaned,
    )


def parse_holdings_text(text: str) -> list[PortfolioHolding]:
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    for raw_line in (text or "").splitlines():
        parsed = _parse_line(raw_line)
        if not parsed:
            continue
        key = (parsed.symbol, parsed.exchange)
        row = grouped.setdefault(
            key,
            {
                "qty": 0.0,
                "cost": 0.0,
                "qty_with_cost": 0.0,
            },
        )
        row["qty"] += parsed.quantity
        if isinstance(parsed.avg_buy_price, (int, float)) and parsed.avg_buy_price > 0:
            q = abs(parsed.quantity)
            row["cost"] += q * float(parsed.avg_buy_price)
            row["qty_with_cost"] += q
    out = [
        PortfolioHolding(
            symbol=symbol,
            exchange=exchange,
            quantity=round(row["qty"], 4),
            avg_buy_price=(
                round(row["cost"] / row["qty_with_cost"], 4) if row["qty_with_cost"] > 0 else None
            ),
            source_line="",
        )
        for (symbol, exchange), row in grouped.items()
        if row["qty"] != 0.0
    ]
    out.sort(key=lambda x: (x.exchange, x.symbol))
    return out


def parse_portfolio_holdings_json(raw: str, *, default_exchange: str = "NSE") -> list[PortfolioHolding]:
    raw_stripped = (raw or "").strip()
    try:
        payload = json.loads(raw_stripped or "[]")
    except json.JSONDecodeError as exc:
        preview = raw_stripped[:200].replace("\n", "\\n")
        raise ValueError(
            "portfolio holdings JSON is invalid (must be a JSON array). "
            f"Parse error at column {exc.colno}: {exc.msg}. Preview: {preview!r}"
        ) from exc
    if not isinstance(payload, list):
        raise ValueError("portfolio holdings JSON must be an array")
    if len(payload) > 300:
        raise ValueError("portfolio holdings JSON exceeds max size (300)")
    rows: list[PortfolioHolding] = []
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"portfolio holding #{idx} must be an object")
        symbol_raw = str(item.get("symbol") or "").strip()
        exchange_raw = str(item.get("exchange") or "").strip().upper() or default_exchange
        qty_raw = item.get("quantity", item.get("qty"))
        if not symbol_raw:
            raise ValueError(f"portfolio holding #{idx} is missing symbol")
        try:
            qty = float(qty_raw)
        except Exception as exc:
            raise ValueError(f"portfolio holding #{idx} has invalid quantity") from exc
        if qty == 0.0 or math.isnan(qty):
            raise ValueError(f"portfolio holding #{idx} quantity must be non-zero")
        buy_price_raw = item.get("avg_buy_price", item.get("buy_price", item.get("avg_price")))
        avg_buy_price: float | None = None
        if buy_price_raw not in (None, ""):
            try:
                avg_buy_price = float(buy_price_raw)
            except Exception as exc:
                raise ValueError(f"portfolio holding #{idx} has invalid avg_buy_price") from exc
            if avg_buy_price <= 0 or math.isnan(avg_buy_price):
                avg_buy_price = None

        if any(sep in symbol_raw for sep in (":", "-", "/")):
            parsed = _parse_symbol_with_exchange(symbol_raw)
            if parsed:
                exch, sym = parsed
                if exch not in _EXCHANGES:
                    exch = default_exchange
                rows.append(
                    PortfolioHolding(
                        symbol=sym,
                        exchange=exch,
                        quantity=qty,
                        avg_buy_price=avg_buy_price,
                        source_line=symbol_raw,
                    )
                )
                continue
        sym = _normalize_symbol(symbol_raw)
        if not _SYMBOL_RE.match(sym):
            raise ValueError(f"portfolio holding #{idx} has invalid symbol")
        exch = exchange_raw if exchange_raw in _EXCHANGES else default_exchange
        rows.append(
            PortfolioHolding(
                symbol=sym,
                exchange=exch,
                quantity=qty,
                avg_buy_price=avg_buy_price,
                source_line=symbol_raw,
            )
        )
    return rows


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
    cfg = load_config()

    session_token = cfg.breeze_session_token
    sup_token = _load_supabase_breeze_token(cfg)
    if sup_token:
        session_token = sup_token

    from breeze_client import create_breeze_session

    class _BreezeCreds:
        breeze_api_key = cfg.breeze_api_key
        breeze_secret = cfg.breeze_secret
        breeze_session_token = session_token

    breeze = create_breeze_session(_BreezeCreds())
    symbol_universe = _load_active_symbol_universe(cfg)
    max_n = max(1, int(max_positions))
    capped = _merge_holdings_resolving_same_ticker(holdings[:max_n], by_exchange=symbol_universe)
    capped = capped[:max_n]
    rows: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted_next_week = 0.0
    weighted_intent = 0.0
    total_invested = 0.0
    total_current = 0.0
    basis_rows = 0
    rollup_positions = 0
    rollup_excluded_outliers = 0

    for h in capped:
        if _holding_is_statement_line(h):
            rows.append(
                {
                    "input_symbol": h.symbol,
                    "input_exchange": h.exchange,
                    "quantity": h.quantity,
                    "status": "ignored_statement_line",
                    "note": "Skipped (broker total / statement token, not a holding)",
                }
            )
            continue
        resolved_symbol, resolved_exchange, map_reason, map_conf = _resolve_symbol(
            h.symbol,
            h.exchange,
            by_exchange=symbol_universe,
        )
        if map_reason == "unresolved":
            rows.append(
                {
                    "input_symbol": h.symbol,
                    "input_exchange": h.exchange,
                    "resolved_symbol": resolved_symbol,
                    "resolved_exchange": resolved_exchange,
                    "mapping_reason": map_reason,
                    "mapping_confidence": map_conf,
                    "quantity": h.quantity,
                    "status": "invalid_symbol_from_pdf",
                }
            )
            continue
        try:
            audit, _ = build_equity_live_audit(
                cfg,
                breeze,
                SectorInstrument(symbol=resolved_symbol, exchange=resolved_exchange),
                sector_id="portfolio_custom",
                with_narrative=False,
                strict_data=False,
            )
            if audit.get("skipped_no_data"):
                rows.append(
                    {
                        "input_symbol": h.symbol,
                        "input_exchange": h.exchange,
                        "resolved_symbol": resolved_symbol,
                        "resolved_exchange": resolved_exchange,
                        "mapping_reason": map_reason,
                        "mapping_confidence": map_conf,
                        "symbol": resolved_symbol,
                        "exchange": resolved_exchange,
                        "quantity": h.quantity,
                        "status": "skipped_no_data",
                    }
                )
                continue
            next_week = audit.get("next_week_score")
            intent = audit.get("effective_intent_score", audit.get("intent_score"))
            current_price = audit.get("close_last")
            sell_signal = str(audit.get("sell_signal") or "hold")
            sell_signal_risk = audit.get("sell_signal_risk_score")
            sell_signal_reasons = audit.get("sell_signal_reasons") if isinstance(audit.get("sell_signal_reasons"), list) else []
            avg_buy = float(h.avg_buy_price) if isinstance(h.avg_buy_price, (int, float)) else None
            q_abs = abs(float(h.quantity))
            invested_value = (avg_buy * q_abs) if isinstance(avg_buy, float) and avg_buy > 0 else None
            current_value = (
                float(current_price) * q_abs if isinstance(current_price, (int, float)) and not math.isnan(float(current_price)) else None
            )
            pnl_abs = (
                (current_value - invested_value)
                if isinstance(current_value, (int, float)) and isinstance(invested_value, (int, float))
                else None
            )
            pnl_pct = (
                ((pnl_abs / invested_value) * 100.0)
                if isinstance(pnl_abs, (int, float)) and isinstance(invested_value, (int, float)) and invested_value != 0
                else None
            )
            cp_f = (
                float(current_price)
                if isinstance(current_price, (int, float)) and not math.isnan(float(current_price))
                else None
            )
            pnl_pct_f = float(pnl_pct) if isinstance(pnl_pct, (int, float)) else None
            basis_unreliable = _cost_basis_unreliable(
                avg_buy=avg_buy,
                current_price=cp_f,
                pnl_pct=pnl_pct_f,
            )
            pnl_pct_display = (
                None
                if basis_unreliable
                else (round(float(pnl_pct_f), 2) if pnl_pct_f is not None else None)
            )
            if isinstance(invested_value, (int, float)) and isinstance(current_value, (int, float)):
                basis_rows += 1
                shape_ok = _rollup_includes_position(invested_value, current_value)
                if shape_ok and not basis_unreliable:
                    total_invested += float(invested_value)
                    total_current += float(current_value)
                    rollup_positions += 1
                else:
                    rollup_excluded_outliers += 1
            weight = abs(float(h.quantity))
            total_weight += weight
            if isinstance(next_week, (int, float)):
                weighted_next_week += float(next_week) * weight
            if isinstance(intent, (int, float)):
                weighted_intent += float(intent) * weight
            action_reasons: list[str] = []
            action_tag = "hold"
            if sell_signal == "exit-risk":
                action_tag = "exit_risk"
                action_reasons.append("sell_signal=exit-risk")
            elif sell_signal == "trim":
                action_tag = "trim"
                action_reasons.append("sell_signal=trim")
            elif (
                not basis_unreliable
                and isinstance(pnl_pct_f, (int, float))
                and pnl_pct_f <= -8
            ):
                action_tag = "exit_risk"
                action_reasons.append("drawdown <= -8%")
            elif (
                not basis_unreliable
                and sell_signal == "hold"
                and isinstance(next_week, (int, float))
                and isinstance(intent, (int, float))
                and float(next_week) >= 70.0
                and float(intent) >= 65.0
                and (pnl_pct_f is None or float(pnl_pct_f) <= 20.0)
            ):
                action_tag = "buy_more"
                action_reasons.append("trend persistence + intent support")
            else:
                action_tag = "hold"
                action_reasons.append("no strong sell risk")

            roll_included = (
                isinstance(invested_value, (int, float))
                and isinstance(current_value, (int, float))
                and _rollup_includes_position(invested_value, current_value)
                and not basis_unreliable
            )
            rows.append(
                {
                    "input_symbol": h.symbol,
                    "input_exchange": h.exchange,
                    "resolved_symbol": resolved_symbol,
                    "resolved_exchange": resolved_exchange,
                    "mapping_reason": map_reason,
                    "mapping_confidence": map_conf,
                    "symbol": resolved_symbol,
                    "exchange": resolved_exchange,
                    "quantity": h.quantity,
                    "avg_buy_price": avg_buy,
                    "status": "ok",
                    "next_week_score": next_week,
                    "intent_score": intent,
                    "z_score": audit.get("z_score"),
                    "return_1d_pct": audit.get("return_1d_pct"),
                    "absorption_ratio": audit.get("absorption_ratio"),
                    "current_price": current_price,
                    "invested_value": invested_value,
                    "current_value": current_value,
                    "unrealized_pnl_value": pnl_abs,
                    "unrealized_pnl_pct": pnl_pct_display,
                    "cost_basis_unreliable": basis_unreliable,
                    "sell_signal": sell_signal,
                    "sell_signal_risk_score": sell_signal_risk,
                    "sell_signal_reasons": sell_signal_reasons,
                    "action_tag": action_tag,
                    "action_reasons": action_reasons,
                    "included_in_headline_rollup": roll_included,
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "input_symbol": h.symbol,
                    "input_exchange": h.exchange,
                    "resolved_symbol": resolved_symbol,
                    "resolved_exchange": resolved_exchange,
                    "mapping_reason": map_reason,
                    "mapping_confidence": map_conf,
                    "symbol": resolved_symbol,
                    "exchange": resolved_exchange,
                    "quantity": h.quantity,
                    "avg_buy_price": h.avg_buy_price,
                    "status": "error",
                    "error": str(exc),
                }
            )

    ok_rows = [r for r in rows if r.get("status") == "ok"]
    skipped_rows = [r for r in rows if r.get("status") == "skipped_no_data"]
    err_rows = [r for r in rows if r.get("status") == "error"]
    invalid_rows = [r for r in rows if r.get("status") == "invalid_symbol_from_pdf"]
    ignored_statement = [r for r in rows if r.get("status") == "ignored_statement_line"]
    summary = {
        "requested_positions": len(capped),
        "analyzed_positions": len(ok_rows),
        "skipped_no_data": len(skipped_rows),
        "invalid_symbol_mappings": len(invalid_rows),
        "errors": len(err_rows),
        "ignored_statement_lines": len(ignored_statement),
        "portfolio_weighted_next_week_score": round(weighted_next_week / total_weight, 2)
        if total_weight > 0
        else None,
        "portfolio_weighted_intent_score": round(weighted_intent / total_weight, 2)
        if total_weight > 0
        else None,
        "positions_with_cost_basis": basis_rows,
        "portfolio_rollup_positions": rollup_positions,
        "portfolio_rollup_excluded_outliers": rollup_excluded_outliers,
        "portfolio_invested_value": round(total_invested, 2) if rollup_positions else None,
        "portfolio_current_value": round(total_current, 2) if rollup_positions else None,
        "portfolio_unrealized_pnl_value": round(total_current - total_invested, 2) if rollup_positions else None,
        "portfolio_unrealized_pnl_pct": round(((total_current - total_invested) / total_invested) * 100.0, 2)
        if rollup_positions and total_invested != 0
        else None,
        "action_counts": {
            "buy_more": sum(1 for r in ok_rows if r.get("action_tag") == "buy_more"),
            "hold": sum(1 for r in ok_rows if r.get("action_tag") == "hold"),
            "trim": sum(1 for r in ok_rows if r.get("action_tag") == "trim"),
            "exit_risk": sum(1 for r in ok_rows if r.get("action_tag") == "exit_risk"),
        },
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


_ACTION_DIGEST_LABEL = {
    "buy_more": "ADD — buy more",
    "hold": "HOLD",
    "trim": "TRIM — take profits",
    "exit_risk": "EXIT RISK — cut exposure",
}


def _format_inr_plain(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(v):
        return "—"
    ax = abs(v)
    if ax >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if ax >= 1e5:
        return f"₹{v / 1e5:,.2f} L"
    return f"₹{v:,.0f}"


def portfolio_email_digest_plaintext(
    *,
    source: str,
    limitations: list[str],
    parsed_count: int,
    result: dict[str, Any],
) -> str:
    """
    Human-readable body for email/UI. Uses same section headers as email_notify HTML heuristic.
    """
    summary = result.get("summary") or {}
    ok_rows = [r for r in (result.get("rows") or []) if r.get("status") == "ok"]
    ac = summary.get("action_counts") or {}

    def sec(title: str) -> list[str]:
        return ["", f"--- {title} ---", ""]

    out: list[str] = []

    out.extend(sec("Overview"))
    out.append(f"Titan portfolio digest ({source}). Holdings lines submitted: {parsed_count}.")

    out.extend(sec("Coverage"))
    out.append(f"Analyzed: {summary.get('analyzed_positions')}")
    out.append(f"No market data: {summary.get('skipped_no_data')}")
    out.append(f"Invalid / unresolved symbols: {summary.get('invalid_symbol_mappings')}")
    out.append(f"Run errors: {summary.get('errors')}")
    isl = summary.get("ignored_statement_lines") or 0
    if isl:
        out.append(f"PDF statement/total lines skipped: {isl}")
    nw = summary.get("portfolio_weighted_next_week_score")
    wi = summary.get("portfolio_weighted_intent_score")
    if isinstance(nw, (int, float)) and isinstance(wi, (int, float)):
        out.append(f"Qty-weighted scores — next week: {nw} | intent: {wi}")

    out.extend(sec("Headline P/L (unrealized, where cost basis is trusted)"))
    rp = int(summary.get("portfolio_rollup_positions") or 0)
    br = int(summary.get("positions_with_cost_basis") or 0)
    bo = int(summary.get("portfolio_rollup_excluded_outliers") or 0)
    if rp:
        out.append(f"Rows with cost × qty: {br} | used in portfolio totals: {rp}")
        if bo:
            out.append(
                f"(Excluded {bo} row(s) with impossible notionals — typical garbled PDF qty/avg price.)",
            )
        out.append(f"Invested (sum): {_format_inr_plain(summary.get('portfolio_invested_value'))}")
        out.append(f"Current value (sum): {_format_inr_plain(summary.get('portfolio_current_value'))}")
        pnl = summary.get("portfolio_unrealized_pnl_value")
        pctp = summary.get("portfolio_unrealized_pnl_pct")
        out.append(
            f"Unrealized P/L: {_format_inr_plain(pnl)}"
            + (f" ({pctp:+.2f}% vs invested rollup)" if isinstance(pctp, (int, float)) else ""),
        )
    else:
        out.append(
            "No trustworthy portfolio-level P/L (add average buy prices per line, "
            "and remove broker TOTAL rows from extracted text). "
            "Per-symbol rows below still show Titan actions where data exists.",
        )

    out.extend(sec("Titan protocol — what to do"))
    out.append(_ACTION_DIGEST_LABEL["exit_risk"])
    out.append(_ACTION_DIGEST_LABEL["trim"])
    out.append(_ACTION_DIGEST_LABEL["hold"])
    out.append(_ACTION_DIGEST_LABEL["buy_more"])
    out.append("")
    out.append("Your mix this run:")
    out.append(
        f"  Exit risk: {ac.get('exit_risk', 0)} | Trim: {ac.get('trim', 0)} | "
        f"Hold: {ac.get('hold', 0)} | Add: {ac.get('buy_more', 0)}",
    )

    out.extend(sec("Per-symbol metrics"))
    out.append("SYMBOL | Titan action | Unrl P/L % | NextWk | Sell-signal note")
    prio = {"exit_risk": 0, "trim": 1, "hold": 2, "buy_more": 3}

    def nw_val(r: dict[str, Any]) -> float:
        v = r.get("next_week_score")
        return float(v) if isinstance(v, (int, float)) else -999.0

    ordered = sorted(
        ok_rows,
        key=lambda r: (prio.get(str(r.get("action_tag")), 9), -nw_val(r)),
    )
    max_rows = 40
    for r in ordered[:max_rows]:
        sym = str(r.get("symbol") or r.get("input_symbol") or "?")
        tag = str(r.get("action_tag") or "hold")
        label = _ACTION_DIGEST_LABEL.get(tag, tag)
        pnl_pct = r.get("unrealized_pnl_pct")
        if r.get("cost_basis_unreliable"):
            pls = "n/a (verify avg buy)"
        else:
            pls = f"{float(pnl_pct):+.1f}%" if isinstance(pnl_pct, (int, float)) else "n/a"
        nws = r.get("next_week_score")
        nwx = f"{float(nws):.1f}" if isinstance(nws, (int, float)) else "—"
        ss = r.get("sell_signal_reasons")
        reason = ""
        if isinstance(ss, list) and ss:
            reason = str(ss[0])
        elif r.get("action_reasons"):
            ar = r.get("action_reasons")
            if isinstance(ar, list) and ar:
                reason = str(ar[0])
        reason = (reason or "")[:96]
        rollup_flag = ""
        if r.get("cost_basis_unreliable"):
            rollup_flag = " ‡"
        elif isinstance(r.get("included_in_headline_rollup"), bool) and not r.get("included_in_headline_rollup"):
            if isinstance(pnl_pct, (int, float)) or r.get("invested_value") is not None:
                rollup_flag = " *"
        out.append(f"{sym} | {label} | {pls} | {nwx} | {reason}{rollup_flag}")
    if len(ordered) > max_rows:
        out.append(f"... ({len(ordered) - max_rows} more rows in full JSON export if needed)")
    if any(r.get("included_in_headline_rollup") is False for r in ordered[:max_rows]) or any(
        r.get("cost_basis_unreliable") for r in ordered[:max_rows]
    ):
        out.append(
            "* = excluded from headline P/L (outlier qty/avg). "
            "‡ = average buy looks wrong vs last price — check your contract note.",
        )

    skipped = [r for r in result.get("rows") or [] if r.get("status") in ("skipped_no_data", "invalid_symbol_from_pdf")]
    if skipped[:12]:
        out.extend(sec("Needs your review (no trade view)"))
        for r in skipped[:12]:
            out.append(
                f"- {r.get('input_symbol')} → {r.get('resolved_symbol')} "
                f"[{r.get('status')}]",
            )

    if limitations:
        out.extend(sec("Parse / input notes"))
        for lim in limitations:
            out.append(f"- {lim}")

    out.extend(sec("Disclaimer"))
    out.append("Not investment advice. Validate symbols, quantities, and averages against your statement.")
    return "\n".join(line for line in out if line is not None).rstrip() + "\n"


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

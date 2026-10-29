"""Pure alert rules: given prices and the user's thresholds, decide which alerts are active.

The bot never decides what is "worth buying"; it only compares prices with the numbers the
user configured. No network or clock access here, so the logic is easy to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional

from .config import Config, HoldingRule, WatchRule
from .toss import Holding, Quote

KR_SYMBOL_RE = re.compile(r"^(?=.*\d)[0-9A-Z]{6}$")   # 6 chars incl. a digit: 005930, 0101N0


def market_of(symbol: str) -> str:
    """KR for 6-character Korean codes, US otherwise (documented symbol formats)."""
    return "KR" if KR_SYMBOL_RE.match(symbol) else "US"


def money(value: Decimal, currency: str) -> str:
    if currency == "KRW":
        return f"{value.quantize(Decimal(1), ROUND_HALF_UP):,}원"
    if currency == "USD":
        return f"${value.quantize(Decimal('0.01'), ROUND_HALF_UP):,}"
    return f"{value:,} {currency}".strip()


def pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1'), ROUND_HALF_UP):+}%"


@dataclass(frozen=True)
class Alert:
    key: str      # stable id for de-duplication, e.g. "TP:005930"
    text: str     # plain text (Telegram is sent without parse_mode)


def evaluate_watch(rules: Iterable[WatchRule], quotes: Dict[str, Quote]) -> List[Alert]:
    alerts: List[Alert] = []
    for r in rules:
        q = quotes.get(r.symbol)
        if not q or q.last_price is None:
            continue
        label = f"{r.name}({r.symbol})"
        price = money(q.last_price, q.currency)
        if r.buy_below is not None and q.last_price <= r.buy_below:
            alerts.append(Alert(f"BUY_BELOW:{r.symbol}",
                                f"[매수 검토] {label}\n현재가 {price} ≤ 설정가 {money(r.buy_below, q.currency)}"))
        if r.buy_above is not None and q.last_price >= r.buy_above:
            alerts.append(Alert(f"BUY_ABOVE:{r.symbol}",
                                f"[돌파 매수 검토] {label}\n현재가 {price} ≥ 설정가 {money(r.buy_above, q.currency)}"))
    return alerts


def evaluate_holdings(holdings: Iterable[Holding], default: HoldingRule,
                      overrides: Dict[str, HoldingRule], ignore: Iterable[str] = ()) -> List[Alert]:
    skip = set(ignore)
    alerts: List[Alert] = []
    for h in holdings:
        if h.symbol in skip or h.quantity <= 0 or h.last_price is None:
            continue
        rule = overrides.get(h.symbol, default)
        label = f"{h.name}({h.symbol}) {h.quantity.normalize():f}주"
        price = money(h.last_price, h.currency)
        rate: Optional[Decimal] = None
        if h.average_price and h.average_price > 0:
            rate = (h.last_price - h.average_price) / h.average_price * 100
        basis = f"평단 {money(h.average_price, h.currency)} → 현재 {price}" if h.average_price else f"현재 {price}"

        if rate is not None and rule.take_profit_pct is not None and rate >= rule.take_profit_pct:
            alerts.append(Alert(f"TP:{h.symbol}",
                                f"[익절 검토] {label}\n수익률 {pct(rate)} (기준 {pct(rule.take_profit_pct)})\n{basis}"))
        if rate is not None and rule.stop_loss_pct is not None and rate <= rule.stop_loss_pct:
            alerts.append(Alert(f"SL:{h.symbol}",
                                f"[손절 검토] {label}\n수익률 {pct(rate)} (기준 {pct(rule.stop_loss_pct)})\n{basis}"))
        if rule.sell_above is not None and h.last_price >= rule.sell_above:
            alerts.append(Alert(f"SELL_ABOVE:{h.symbol}",
                                f"[목표가 도달] {label}\n현재가 {price} ≥ 목표가 {money(rule.sell_above, h.currency)}"))
        if rule.sell_below is not None and h.last_price <= rule.sell_below:
            alerts.append(Alert(f"SELL_BELOW:{h.symbol}",
                                f"[하한가 이탈] {label}\n현재가 {price} ≤ 기준가 {money(rule.sell_below, h.currency)}"))
    return alerts


def holdings_for_open_markets(holdings: Iterable[Holding], cfg: Config, now) -> List[Holding]:
    return [h for h in holdings if cfg.market_open(market_of(h.symbol), now)]


def watch_for_open_markets(cfg: Config, now) -> List[WatchRule]:
    return [w for w in cfg.watch if cfg.market_open(market_of(w.symbol), now)]

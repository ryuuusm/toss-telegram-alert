"""Toss Securities Open API client (read-only subset used by the alert bot).

Endpoints and fields follow the official reference (checked 2026-10-29):
  POST /oauth2/token        OAuth2 client credentials, form-encoded
  GET  /api/v1/accounts     -> result[]: accountNo, accountSeq, accountType
  GET  /api/v1/holdings     header X-Tossinvest-Account -> result.items[]:
                            symbol, name, currency, quantity, lastPrice, averagePurchasePrice
  GET  /api/v1/prices       ?symbols=a,b (max 200) -> result[]: symbol, lastPrice, currency, timestamp
Only one access token is valid per client at a time: issuing a new one revokes the old one,
so the bot keeps a single token in memory and re-issues it only after a 401.
This client never calls order endpoints.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from .http import HttpError, Transport, request_json, urllib_transport

BASE_URL = "https://openapi.tossinvest.com"
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")      # documented allowed characters
MAX_SYMBOLS_PER_CALL = 200
TOKEN_ERROR_CODES = {"expired-token", "invalid-token", "token-revoked"}


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    currency: str
    quantity: Decimal
    last_price: Optional[Decimal]
    average_price: Optional[Decimal]


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_price: Optional[Decimal]
    currency: str


def to_decimal(value: Any) -> Optional[Decimal]:
    """Accept numbers or numeric strings; return None for missing/invalid values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid symbol: {symbol!r}")
    return symbol


class TossClient:
    def __init__(self, client_id: str, client_secret: str, account_seq: Optional[int] = None,
                 transport: Transport = urllib_transport, base_url: str = BASE_URL, **request_kw: Any):
        if not client_id or not client_secret:
            raise ValueError("TOSS_CLIENT_ID and TOSS_CLIENT_SECRET are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_seq = account_seq
        self._transport = transport
        self._base = base_url.rstrip("/")
        self._kw = request_kw          # e.g. sleep=... in tests
        self._token: Optional[str] = None

    # -- auth ---------------------------------------------------------------
    def _issue_token(self) -> str:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode()
        data = request_json(self._transport, "POST", f"{self._base}/oauth2/token",
                            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                            body, **self._kw)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise HttpError(0, "no-access-token", "token response had no access_token")
        self._token = token
        return token

    def _get(self, path: str, params: Optional[Dict[str, str]] = None, account: bool = False) -> Any:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in (1, 2):
            token = self._token or self._issue_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if account:
                headers["X-Tossinvest-Account"] = str(self.account_seq())
            try:
                return request_json(self._transport, "GET", url, headers, **self._kw)
            except HttpError as exc:
                # Re-issue once on an expired/revoked token; anything else propagates.
                if attempt == 1 and exc.status == 401 and (exc.code in TOKEN_ERROR_CODES or not exc.code):
                    self._token = None
                    continue
                raise
        raise AssertionError("unreachable")

    # -- account ------------------------------------------------------------
    def accounts(self) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/accounts")
        result = data.get("result") if isinstance(data, dict) else None
        return [a for a in (result or []) if isinstance(a, dict)]

    def account_seq(self) -> int:
        """Configured accountSeq, or the first BROKERAGE account found."""
        if self._account_seq is None:
            accts = self.accounts()
            brokerage = [a for a in accts if a.get("accountType") == "BROKERAGE"] or accts
            if not brokerage or brokerage[0].get("accountSeq") is None:
                raise HttpError(0, "no-account", "no brokerage account returned by /api/v1/accounts")
            self._account_seq = int(brokerage[0]["accountSeq"])
        return self._account_seq

    # -- data -----------------------------------------------------------------
    def holdings(self) -> List[Holding]:
        data = self._get("/api/v1/holdings", account=True)
        result = data.get("result") if isinstance(data, dict) else None
        items = (result or {}).get("items") or []
        out: List[Holding] = []
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("symbol"), str):
                continue
            out.append(Holding(
                symbol=it["symbol"],
                name=str(it.get("name") or it["symbol"]),
                currency=str(it.get("currency") or ""),
                quantity=to_decimal(it.get("quantity")) or Decimal(0),
                last_price=to_decimal(it.get("lastPrice")),
                average_price=to_decimal(it.get("averagePurchasePrice")),
            ))
        return out

    def prices(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        syms = list(dict.fromkeys(validate_symbol(s) for s in symbols))
        quotes: Dict[str, Quote] = {}
        for i in range(0, len(syms), MAX_SYMBOLS_PER_CALL):
            chunk = syms[i:i + MAX_SYMBOLS_PER_CALL]
            data = self._get("/api/v1/prices", {"symbols": ",".join(chunk)})
            for p in (data.get("result") if isinstance(data, dict) else None) or []:
                if isinstance(p, dict) and isinstance(p.get("symbol"), str):
                    quotes[p["symbol"]] = Quote(p["symbol"], to_decimal(p.get("lastPrice")),
                                                str(p.get("currency") or ""))
        return quotes

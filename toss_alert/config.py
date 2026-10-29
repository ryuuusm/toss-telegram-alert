"""Configuration: secrets from environment/.env, rules from a TOML file, all validated up front."""
from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .toss import validate_symbol

TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------- secrets

@dataclass(frozen=True)
class Secrets:
    toss_client_id: str
    toss_client_secret: str
    telegram_bot_token: str
    telegram_chat_id: str
    toss_account_seq: Optional[int]

    def values(self) -> Tuple[str, ...]:
        """Every secret string, for log redaction."""
        return (self.toss_client_id, self.toss_client_secret, self.telegram_bot_token)


def load_dotenv(path: Path) -> None:
    """Tiny .env reader: KEY=VALUE lines, '#' comments, optional quotes. Never overrides real env vars."""
    if not path.is_file():
        return
    if os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"경고: {path} 권한이 너무 넓습니다. 'chmod 600 {path}'로 본인만 읽게 하세요.", file=sys.stderr)
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_secrets(require_telegram: bool = True) -> Secrets:
    env = os.environ
    missing = [k for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET") if not env.get(k)]
    if require_telegram:
        missing += [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k)]
    if missing:
        raise ConfigError("환경 변수가 없습니다: " + ", ".join(missing) + " (.env.example 참고)")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if require_telegram and not TELEGRAM_TOKEN_RE.match(token):
        raise ConfigError("TELEGRAM_BOT_TOKEN 형식이 올바르지 않습니다 (BotFather가 준 '숫자:문자열')")
    if require_telegram and not CHAT_ID_RE.match(chat):
        raise ConfigError("TELEGRAM_CHAT_ID는 숫자여야 합니다")
    seq_raw = env.get("TOSS_ACCOUNT_SEQ", "").strip()
    if seq_raw and not seq_raw.isdigit():
        raise ConfigError("TOSS_ACCOUNT_SEQ는 숫자여야 합니다")
    return Secrets(env["TOSS_CLIENT_ID"].strip(), env["TOSS_CLIENT_SECRET"].strip(), token.strip(),
                   chat.strip(), int(seq_raw) if seq_raw else None)


# ---------------------------------------------------------------- rules config

@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def contains(self, now: datetime, weekdays_only: bool) -> bool:
        """True if `now` falls in the window. Windows may cross midnight (e.g. 22:30-06:00);
        then the after-midnight part belongs to the previous day for the weekday check."""
        t = now.time()
        def day_ok(d: datetime) -> bool:
            return not weekdays_only or d.weekday() < 5
        if self.start < self.end:
            return self.start <= t < self.end and day_ok(now)
        if t >= self.start:
            return day_ok(now)
        if t < self.end:
            return day_ok(now - timedelta(days=1))
        return False


@dataclass(frozen=True)
class WatchRule:
    symbol: str
    name: str
    buy_below: Optional[Decimal]
    buy_above: Optional[Decimal]


@dataclass(frozen=True)
class HoldingRule:
    take_profit_pct: Optional[Decimal]
    stop_loss_pct: Optional[Decimal]
    sell_above: Optional[Decimal] = None
    sell_below: Optional[Decimal] = None


@dataclass(frozen=True)
class Config:
    interval_seconds: int
    cooldown_minutes: int
    timezone: ZoneInfo
    weekdays_only: bool
    hours: Dict[str, List[Window]]                 # market ("KR"/"US") -> windows
    holdings_enabled: bool
    holding_default: HoldingRule
    holding_overrides: Dict[str, HoldingRule]
    ignore_symbols: Tuple[str, ...]
    watch: List[WatchRule] = field(default_factory=list)
    state_file: Path = Path("alert_state.json")

    def market_open(self, market: str, now: datetime) -> bool:
        return any(w.contains(now, self.weekdays_only) for w in self.hours.get(market, []))


def _dec(value: Any, where: str, *, positive: bool = False) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{where}: 숫자여야 합니다")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{where}: 숫자여야 합니다") from exc
    if not d.is_finite() or (positive and d <= 0):
        raise ConfigError(f"{where}: 0보다 큰 숫자여야 합니다")
    return d


def _windows(values: Any, where: str) -> List[Window]:
    if not isinstance(values, list):
        raise ConfigError(f"{where}: [\"HH:MM-HH:MM\", ...] 목록이어야 합니다")
    out = []
    for v in values:
        m = WINDOW_RE.match(v) if isinstance(v, str) else None
        if not m:
            raise ConfigError(f"{where}: '{v}'는 HH:MM-HH:MM 형식이어야 합니다")
        s, e = time(int(m[1]), int(m[2])), time(int(m[3]), int(m[4]))
        if s == e:
            raise ConfigError(f"{where}: 시작과 끝이 같습니다")
        out.append(Window(s, e))
    return out


def _holding_rule(raw: Dict[str, Any], where: str, base: Optional[HoldingRule] = None) -> HoldingRule:
    tp = _dec(raw.get("take_profit_pct"), f"{where}.take_profit_pct")
    sl = _dec(raw.get("stop_loss_pct"), f"{where}.stop_loss_pct")
    if tp is not None and tp <= 0:
        raise ConfigError(f"{where}.take_profit_pct는 양수여야 합니다 (예: 15)")
    if sl is not None and sl >= 0:
        raise ConfigError(f"{where}.stop_loss_pct는 음수여야 합니다 (예: -8)")
    rule = HoldingRule(
        take_profit_pct=tp if "take_profit_pct" in raw else (base.take_profit_pct if base else None),
        stop_loss_pct=sl if "stop_loss_pct" in raw else (base.stop_loss_pct if base else None),
        sell_above=_dec(raw.get("sell_above"), f"{where}.sell_above", positive=True),
        sell_below=_dec(raw.get("sell_below"), f"{where}.sell_below", positive=True),
    )
    if rule.sell_above and rule.sell_below and rule.sell_below >= rule.sell_above:
        raise ConfigError(f"{where}: sell_below는 sell_above보다 작아야 합니다")
    return rule


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없습니다: {path} (config.example.toml을 복사하세요)") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"설정 파일 문법 오류: {exc}") from exc

    g = raw.get("general", {})
    interval = g.get("interval_seconds", 60)
    if not isinstance(interval, int) or isinstance(interval, bool) or not 10 <= interval <= 3600:
        raise ConfigError("general.interval_seconds는 10~3600 정수여야 합니다")
    cooldown = g.get("cooldown_minutes", 60)
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or not 1 <= cooldown <= 10080:
        raise ConfigError("general.cooldown_minutes는 1~10080 정수여야 합니다")
    try:
        tz = ZoneInfo(str(g.get("timezone", "Asia/Seoul")))
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError
        raise ConfigError("general.timezone이 올바르지 않습니다 (예: Asia/Seoul)") from exc
    hours_raw = raw.get("hours", {"KR": ["08:00-20:00"]})
    if not isinstance(hours_raw, dict):
        raise ConfigError("[hours]는 KR/US 키를 가진 표여야 합니다")
    hours = {}
    for market, wins in hours_raw.items():
        if market not in ("KR", "US"):
            raise ConfigError(f"[hours]의 키는 KR 또는 US만 가능합니다: {market}")
        hours[market] = _windows(wins, f"hours.{market}")

    h = raw.get("holdings", {})
    if "sell_above" in h or "sell_below" in h:
        raise ConfigError("sell_above/sell_below는 종목별 [[holdings.override]]에만 쓸 수 있습니다")
    default = _holding_rule(h, "holdings")
    overrides: Dict[str, HoldingRule] = {}
    for i, o in enumerate(h.get("override", [])):
        where = f"holdings.override[{i}]"
        if not isinstance(o, dict):
            raise ConfigError(f"{where}: 표 형식이어야 합니다")
        sym = validate_symbol(str(o.get("symbol", ""))) if o.get("symbol") else None
        if not sym:
            raise ConfigError(f"{where}.symbol이 필요합니다")
        overrides[sym] = _holding_rule(o, where, base=default)
    ignore = tuple(validate_symbol(s) for s in h.get("ignore", []))

    watch: List[WatchRule] = []
    for i, w in enumerate(raw.get("watch", [])):
        where = f"watch[{i}]"
        if not isinstance(w, dict):
            raise ConfigError(f"{where}: 표 형식이어야 합니다")
        try:
            sym = validate_symbol(str(w.get("symbol", "")))
        except ValueError as exc:
            raise ConfigError(f"{where}.symbol이 올바르지 않습니다") from exc
        rule = WatchRule(sym, str(w.get("name") or sym)[:40],
                         _dec(w.get("buy_below"), f"{where}.buy_below", positive=True),
                         _dec(w.get("buy_above"), f"{where}.buy_above", positive=True))
        if rule.buy_below is None and rule.buy_above is None:
            raise ConfigError(f"{where}: buy_below 또는 buy_above 중 하나가 필요합니다")
        watch.append(rule)

    state_file = Path(str(g.get("state_file", "alert_state.json")))
    return Config(interval, cooldown, tz, bool(g.get("weekdays_only", True)), hours,
                  bool(h.get("enabled", True)), default, overrides, ignore, watch, state_file)

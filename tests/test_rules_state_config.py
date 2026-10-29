"""Rules, de-duplication state, config validation, market hours, Telegram, one full cycle."""
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from toss_alert.config import ConfigError, HoldingRule, WatchRule, load_config, load_dotenv, load_secrets
from toss_alert.rules import evaluate_holdings, evaluate_watch, market_of
from toss_alert.runner import run_once
from toss_alert.state import AlertState
from toss_alert.telegram import TelegramNotifier
from toss_alert.toss import Holding, Quote, TossClient

KST = ZoneInfo("Asia/Seoul")
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def H(sym, last, avg, qty=1, cur="KRW", name="X"):
    return Holding(sym, name, cur, Decimal(qty), Decimal(str(last)), Decimal(str(avg)) if avg else None)


# ---- rules -------------------------------------------------------------------
def test_market_of():
    assert market_of("005930") == "KR" and market_of("0101N0") == "KR"
    assert market_of("AAPL") == "US" and market_of("BRK.B") == "US" and market_of("ABCDEF") == "US"


def test_take_profit_and_stop_loss_boundaries():
    rule = HoldingRule(Decimal(15), Decimal(-8))
    keys = lambda hs: [a.key for a in evaluate_holdings(hs, rule, {})]
    assert keys([H("005930", 115, 100)]) == ["TP:005930"]          # exactly +15%
    assert keys([H("005930", 114.9, 100)]) == []
    assert keys([H("005930", 92, 100)]) == ["SL:005930"]           # exactly -8%
    assert keys([H("005930", 92.1, 100)]) == []


def test_override_absolute_targets_and_ignore():
    default = HoldingRule(Decimal(15), Decimal(-8))
    ov = {"005930": HoldingRule(Decimal(50), None, sell_above=Decimal(95000), sell_below=Decimal(60000))}
    alerts = evaluate_holdings([H("005930", 96000, 90000), H("000660", 200, 100)], default, ov, ignore=["000660"])
    assert [a.key for a in alerts] == ["SELL_ABOVE:005930"]
    assert "96,000원" in alerts[0].text and "95,000원" in alerts[0].text


def test_no_alert_without_price_or_quantity():
    rule = HoldingRule(Decimal(1), Decimal(-1))
    assert evaluate_holdings([H("005930", 200, 100, qty=0)], rule, {}) == []
    assert evaluate_holdings([Holding("005930", "X", "KRW", Decimal(1), None, Decimal(100))], rule, {}) == []


def test_watch_rules_and_usd_format():
    rules = [WatchRule("AAPL", "Apple", Decimal(180), None), WatchRule("000660", "SKH", None, Decimal(200000))]
    quotes = {"AAPL": Quote("AAPL", Decimal("179.5"), "USD"), "000660": Quote("000660", Decimal(199000), "KRW")}
    alerts = evaluate_watch(rules, quotes)
    assert [a.key for a in alerts] == ["BUY_BELOW:AAPL"]
    assert "$179.50" in alerts[0].text


# ---- state -------------------------------------------------------------------
def test_state_cooldown_and_rearm(tmp_path):
    from toss_alert.rules import Alert
    st = AlertState(tmp_path / "s.json")
    a = Alert("TP:1", "t")
    assert st.due([a], 1000, 3600) == [a]
    st.mark_sent("TP:1", 1000)
    assert st.due([a], 2000, 3600) == []          # within cooldown
    assert st.due([a], 4600, 3600) == [a]         # cooldown passed
    st.keep_only([])                              # condition no longer true -> re-armed
    assert st.due([a], 2001, 3600) == [a]
    st.mark_sent("TP:1", 1)
    st.save()
    assert json.loads((tmp_path / "s.json").read_text()) == {"TP:1": 1}
    assert AlertState(tmp_path / "s.json").due([a], 100, 3600) == []


def test_corrupt_state_is_ignored(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert AlertState(p).due([], 0, 1) == []


# ---- config ------------------------------------------------------------------
def test_example_config_is_valid():
    cfg = load_config(EXAMPLE)
    assert cfg.holding_default.take_profit_pct == Decimal(15)
    assert cfg.holding_overrides["005930"].sell_above == Decimal(95000)
    assert cfg.holding_overrides["005930"].stop_loss_pct == Decimal(-8)   # inherited
    assert [w.symbol for w in cfg.watch] == ["000660", "AAPL"]


@pytest.mark.parametrize("snippet,msg", [
    ("[general]\ninterval_seconds = 1\n", "interval"),
    ("[holdings]\ntake_profit_pct = -5\n", "take_profit_pct"),
    ("[holdings]\nstop_loss_pct = 5\n", "stop_loss_pct"),
    ("[holdings]\nsell_above = 5\n", "override"),
    ("[[watch]]\nsymbol = \"005930\"\n", "buy_below"),
    ("[[watch]]\nsymbol = \"../etc\"\nbuy_below = 1\n", "symbol"),
    ("[hours]\nKR = [\"25:00-26:00\"]\n", "HH:MM"),
    ("[hours]\nJP = [\"09:00-15:00\"]\n", "KR 또는 US"),
    ("[[holdings.override]]\nsymbol = \"005930\"\nsell_above = 10\nsell_below = 20\n", "sell_below"),
])
def test_invalid_config_rejected(tmp_path, snippet, msg):
    p = tmp_path / "c.toml"
    p.write_text(snippet, encoding="utf-8")
    with pytest.raises((ConfigError, ValueError)) as ei:
        load_config(p)
    assert msg in str(ei.value)


def test_market_hours_cross_midnight(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[hours]\nKR = ["08:00-20:00"]\nUS = ["22:30-05:00"]\n', encoding="utf-8")
    cfg = load_config(p)
    fri_night = datetime(2026, 10, 30, 23, 0, tzinfo=KST)    # Friday
    sat_early = datetime(2026, 10, 31, 3, 0, tzinfo=KST)     # belongs to Friday's US session
    sun_early = datetime(2026, 11, 1, 3, 0, tzinfo=KST)      # belongs to Saturday -> closed
    assert cfg.market_open("US", fri_night) and cfg.market_open("US", sat_early)
    assert not cfg.market_open("US", sun_early)
    assert cfg.market_open("KR", datetime(2026, 10, 29, 9, 0, tzinfo=KST))
    assert not cfg.market_open("KR", datetime(2026, 10, 31, 9, 0, tzinfo=KST))   # Saturday


def test_dotenv_and_secret_validation(tmp_path, monkeypatch):
    for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TOSS_ACCOUNT_SEQ"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('# c\nTOSS_CLIENT_ID="tsck_live_x"\nTOSS_CLIENT_SECRET=s\n'
                   'TELEGRAM_BOT_TOKEN=123456:' + "A" * 35 + '\nTELEGRAM_CHAT_ID=-100123\n', encoding="utf-8")
    env.chmod(0o600)
    load_dotenv(env)
    s = load_secrets()
    assert s.toss_client_id == "tsck_live_x" and s.telegram_chat_id == "-100123" and s.toss_account_seq is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    with pytest.raises(ConfigError):
        load_secrets()


# ---- telegram + full cycle ----------------------------------------------------
def test_telegram_plain_text_payload(fake, no_sleep):
    fake.add("POST", "telegram", body={"ok": True})
    TelegramNotifier("123:" + "A" * 35, "42", transport=fake, **no_sleep).send("<b>x</b> & [link](http://e)")
    _, url, headers, body = fake.calls[0]
    payload = json.loads(body)
    assert "parse_mode" not in payload and payload["text"] == "<b>x</b> & [link](http://e)"
    assert payload["chat_id"] == "42"


def test_run_once_sends_then_suppresses(tmp_path, fake, no_sleep):
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text('[general]\ncooldown_minutes = 60\n[hours]\nKR = ["08:00-20:00"]\n'
                        '[holdings]\ntake_profit_pct = 10\nstop_loss_pct = -10\n'
                        '[[watch]]\nsymbol = "000660"\nbuy_below = 200000\n', encoding="utf-8")
    cfg = load_config(cfg_path)
    fake.add("POST", "/oauth2/token", body={"access_token": "t"})
    fake.add("GET", "/api/v1/accounts", body={"result": [{"accountSeq": 1, "accountType": "BROKERAGE"}]})
    fake.add("GET", "/api/v1/holdings", body={"result": {"items": [
        {"symbol": "005930", "name": "삼성전자", "currency": "KRW", "quantity": 1,
         "lastPrice": 110, "averagePurchasePrice": 100}]}})
    fake.add("GET", "/api/v1/prices", body={"result": [{"symbol": "000660", "lastPrice": 190000, "currency": "KRW"}]})
    sent = []

    class N:
        def send(self, text): sent.append(text)

    toss = TossClient("c", "s", transport=fake, **no_sleep)
    state = AlertState(tmp_path / "st.json")
    t0 = datetime(2026, 10, 29, 10, 0, tzinfo=KST)
    assert run_once(cfg, toss, N(), state, t0) == 2
    assert run_once(cfg, toss, N(), state, t0.replace(minute=30)) == 0       # cooldown
    assert run_once(cfg, toss, N(), state, t0.replace(hour=11, minute=1)) == 2
    assert run_once(cfg, toss, N(), state, t0.replace(hour=21)) == 0          # market closed
    assert any("[익절 검토]" in t for t in sent) and any("[매수 검토]" in t for t in sent)
    assert all("자동 주문은 하지 않으며" in t for t in sent)

"""TossClient: token handling, account header, retries, parsing (all with a fake transport)."""
from decimal import Decimal

import pytest

from toss_alert.http import HttpError
from toss_alert.toss import TossClient

TOKEN = {"access_token": "tok-1", "token_type": "Bearer"}
ACCOUNTS = {"result": [{"accountNo": "123", "accountSeq": 7, "accountType": "BROKERAGE"}]}
HOLDINGS = {"result": {"items": [
    {"symbol": "005930", "name": "삼성전자", "currency": "KRW", "quantity": 10,
     "lastPrice": 70000, "averagePurchasePrice": 60000.5},
    {"symbol": "AAPL", "name": "Apple", "currency": "USD", "quantity": "1.5",
     "lastPrice": "190.25", "averagePurchasePrice": "200"},
    {"bad": "row"},
]}}


def client(fake, no_sleep, **kw):
    return TossClient("cid", "secret", transport=fake, **kw, **no_sleep)


def test_token_form_and_account_header(fake, no_sleep):
    fake.add("POST", "/oauth2/token", body=TOKEN)
    fake.add("GET", "/api/v1/accounts", body=ACCOUNTS)
    fake.add("GET", "/api/v1/holdings", body=HOLDINGS)
    hs = client(fake, no_sleep).holdings()
    method, url, headers, body = fake.calls[0]
    assert body == b"grant_type=client_credentials&client_id=cid&client_secret=secret"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    hold_call = fake.calls[-1]
    assert hold_call[2]["Authorization"] == "Bearer tok-1"
    assert hold_call[2]["X-Tossinvest-Account"] == "7"
    assert [h.symbol for h in hs] == ["005930", "AAPL"]
    assert hs[0].average_price == Decimal("60000.5") and hs[1].quantity == Decimal("1.5")


def test_token_reissued_once_on_401(fake, no_sleep):
    fake.add("POST", "/oauth2/token", body=TOKEN)
    fake.add("GET", "/api/v1/prices", status=401, body={"error": {"code": "expired-token", "message": "x"}})
    fake.add("GET", "/api/v1/prices", body={"result": [{"symbol": "005930", "lastPrice": 70100, "currency": "KRW"}]})
    q = client(fake, no_sleep).prices(["005930"])
    assert q["005930"].last_price == Decimal(70100)
    assert sum(1 for c in fake.calls if c[1].endswith("/oauth2/token")) == 2


def test_other_4xx_not_retried(fake, no_sleep):
    fake.add("POST", "/oauth2/token", body=TOKEN)
    fake.add("GET", "/api/v1/prices", status=403,
             body={"error": {"code": "ip-not-allowed", "message": "blocked", "requestId": "r1"}})
    with pytest.raises(HttpError) as ei:
        client(fake, no_sleep).prices(["005930"])
    assert ei.value.code == "ip-not-allowed" and ei.value.request_id == "r1"
    assert len([c for c in fake.calls if "/prices" in c[1]]) == 1


def test_429_honours_retry_after(fake):
    waits = []
    fake.add("POST", "/oauth2/token", body=TOKEN)
    fake.add("GET", "/api/v1/prices", status=429, headers={"retry-after": "2"},
             body={"error": {"code": "rate-limit-exceeded"}})
    fake.add("GET", "/api/v1/prices", body={"result": []})
    TossClient("cid", "secret", transport=fake, sleep=waits.append, rand=lambda: 0.0).prices(["005930"])
    assert waits == [2.0]


def test_prices_chunked_by_200(fake, no_sleep):
    fake.add("POST", "/oauth2/token", body=TOKEN)
    fake.add("GET", "/api/v1/prices", body={"result": []})
    syms = [f"{i:06d}" for i in range(450)]
    client(fake, no_sleep).prices(syms)
    assert len([c for c in fake.calls if "/prices" in c[1]]) == 3


def test_invalid_symbol_rejected(fake, no_sleep):
    with pytest.raises(ValueError):
        client(fake, no_sleep).prices(["005930&evil=1"])


def test_oauth_error_has_no_secret(fake, no_sleep):
    fake.add("POST", "/oauth2/token", status=401, body={"error": "invalid_client", "error_description": "bad"})
    with pytest.raises(HttpError) as ei:
        client(fake, no_sleep).holdings()
    assert ei.value.code == "invalid_client" and "secret" not in str(ei.value)


def test_configured_account_seq_skips_lookup(fake, no_sleep):
    fake.add("POST", "/oauth2/token", body=TOKEN)
    fake.add("GET", "/api/v1/holdings", body={"result": {"items": []}})
    assert client(fake, no_sleep, account_seq=3).holdings() == []
    assert not any("/accounts" in c[1] for c in fake.calls)

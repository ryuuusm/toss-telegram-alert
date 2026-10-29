"""Check cycle, polling loop, single-instance lock and command-line interface."""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Protocol

from .config import Config, ConfigError, load_config, load_dotenv, load_secrets
from .http import HttpError, Redactor
from .rules import (Alert, evaluate_holdings, evaluate_watch, holdings_for_open_markets,
                    watch_for_open_markets)
from .state import AlertState
from .telegram import ConsoleNotifier, TelegramNotifier
from .toss import TossClient

log = logging.getLogger("toss_alert")
FOOTER = "\n\n※ 설정한 기준에 따른 알림입니다. 자동 주문은 하지 않으며, 매매 판단은 직접 하세요."
FAILURE_NOTICE_AFTER = 5     # consecutive failed cycles before one "bot is failing" message


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


def run_once(cfg: Config, toss: TossClient, notifier: Notifier, state: AlertState,
             now: Optional[datetime] = None) -> int:
    """One check. Returns the number of alerts sent."""
    now = now or datetime.now(cfg.timezone)
    open_watch = watch_for_open_markets(cfg, now)
    any_market_open = any(cfg.market_open(m, now) for m in cfg.hours)
    if not any_market_open:
        log.debug("모든 시장이 설정한 시간 밖이라 건너뜀")
        return 0

    alerts: List[Alert] = []
    if cfg.holdings_enabled:
        holdings = holdings_for_open_markets(toss.holdings(), cfg, now)
        alerts += evaluate_holdings(holdings, cfg.holding_default, cfg.holding_overrides, cfg.ignore_symbols)
    if open_watch:
        quotes = toss.prices(w.symbol for w in open_watch)
        alerts += evaluate_watch(open_watch, quotes)

    now_ts = now.timestamp()
    sent = 0
    for alert in state.due(alerts, now_ts, cfg.cooldown_minutes * 60):
        notifier.send(alert.text + FOOTER)
        state.mark_sent(alert.key, now_ts)    # only after Telegram confirmed delivery
        sent += 1
    state.keep_only(a.key for a in alerts)    # re-arm conditions that are no longer true
    state.save()
    log.info("확인 완료: 조건 충족 %d건, 발송 %d건", len(alerts), sent)
    return sent


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Only one bot per credentials: a second process would issue a new token and revoke ours."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SystemExit("이미 다른 봇이 실행 중입니다 (lock 파일 사용 중)") from None
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SystemExit("이미 다른 봇이 실행 중입니다 (lock 파일 사용 중)") from None
        yield
    finally:
        fh.close()


def loop(cfg: Config, toss: TossClient, notifier: Notifier, state: AlertState, redact: Redactor) -> None:
    failures = 0
    while True:
        try:
            run_once(cfg, toss, notifier, state)
            if failures >= FAILURE_NOTICE_AFTER:
                _safe_notify(notifier, "[알림봇] 다시 정상 동작합니다.", redact)
            failures = 0
        except (HttpError, OSError, ValueError) as exc:
            failures += 1
            log.warning("확인 실패 (%d회 연속): %s", failures, redact(str(exc)))
            if failures == FAILURE_NOTICE_AFTER:
                _safe_notify(notifier, f"[알림봇] {failures}회 연속 확인에 실패했습니다: {redact(str(exc))}", redact)
        # Small jitter so restarts don't line up with the 09:00 peak exactly.
        time.sleep(cfg.interval_seconds + random.uniform(0, 3))


def _safe_notify(notifier: Notifier, text: str, redact: Redactor) -> None:
    try:
        notifier.send(text)
    except (HttpError, OSError) as exc:
        log.warning("텔레그램 발송 실패: %s", redact(str(exc)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="toss_alert", description="토스증권 시세 → 텔레그램 가격 알림 (주문 기능 없음)")
    ap.add_argument("--config", default="config.toml", help="규칙 설정 파일 (기본 config.toml)")
    ap.add_argument("--env", default=".env", help="비밀 값 파일 (기본 .env)")
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료 (작업 스케줄러용)")
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 대신 화면에 출력")
    ap.add_argument("--check-api", action="store_true", help="토스 API 연결과 계좌·보유 종목 조회만 확인")
    ap.add_argument("--test-telegram", action="store_true", help="텔레그램 테스트 메시지 1건 발송")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        load_dotenv(Path(args.env))
        secrets = load_secrets(require_telegram=not (args.dry_run or args.check_api))
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    redact = Redactor(secrets.values())
    toss = TossClient(secrets.toss_client_id, secrets.toss_client_secret, secrets.toss_account_seq)
    notifier: Notifier = ConsoleNotifier() if args.dry_run else TelegramNotifier(
        secrets.telegram_bot_token, secrets.telegram_chat_id)

    try:
        if args.test_telegram:
            notifier.send("[알림봇] 테스트 메시지입니다. 이 메시지가 보이면 텔레그램 연동이 정상입니다.")
            print("텔레그램 테스트 메시지를 보냈습니다.")
            return 0
        if args.check_api:
            holdings = toss.holdings()
            print(f"토스 API 연결 정상. accountSeq={toss.account_seq()}, 보유 종목 {len(holdings)}개")
            for h in holdings:
                print(f"  {h.symbol} {h.name} 수량 {h.quantity.normalize():f} 평단 {h.average_price} 현재 {h.last_price} {h.currency}")
            if cfg.watch:
                quotes = toss.prices(w.symbol for w in cfg.watch)
                for w in cfg.watch:
                    q = quotes.get(w.symbol)
                    print(f"  [관심] {w.symbol} {w.name} 현재 {q.last_price if q else '조회 안 됨'}")
            return 0
        with single_instance(cfg.state_file.with_suffix(".lock")):
            state = AlertState(cfg.state_file)
            if args.once:
                run_once(cfg, toss, notifier, state)
                return 0
            log.info("알림봇 시작: %d초 간격, 관심 종목 %d개 (Ctrl+C로 종료)", cfg.interval_seconds, len(cfg.watch))
            loop(cfg, toss, notifier, state, redact)
    except KeyboardInterrupt:
        log.info("종료합니다.")
        return 0
    except HttpError as exc:
        msg = redact(str(exc))
        hint = ""
        if exc.status == 403 or exc.code in ("ip-not-allowed", "access_denied"):
            hint = "\n→ WTS 설정 > Open API의 '허용 IP'에 이 컴퓨터의 공인 IP가 등록되어 있는지 확인하세요."
        elif exc.code == "invalid_client":
            hint = "\n→ client_id / client_secret을 다시 복사해 넣으세요 (앞뒤 공백 주의)."
        print(f"API 오류: {msg}{hint}", file=sys.stderr)
        return 1
    return 0

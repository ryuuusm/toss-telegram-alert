# 토스증권 가격 알림봇 (텔레그램)

토스증권 Open API로 **보유 종목**과 **관심 종목**의 시세를 주기적으로 확인하고, 직접 정한 기준에 닿으면 텔레그램으로 알려 줍니다.

- **알림 전용입니다.** 주문 API는 호출하지 않습니다. 매매는 알림을 보고 직접 하세요.
- 어떤 가격이 "살 만한지/팔 만한지"는 봇이 판단하지 않습니다. `config.toml`에 적은 숫자와 비교만 합니다.
- Python 3.11 이상, 외부 패키지 없음 (표준 라이브러리만 사용).

## 어떤 알림이 오나

| 알림 | 조건 (config.toml) |
|------|------|
| [매수 검토] | 관심 종목 현재가 ≤ `buy_below` |
| [돌파 매수 검토] | 관심 종목 현재가 ≥ `buy_above` |
| [익절 검토] | 보유 종목 수익률(평단 대비) ≥ `take_profit_pct` |
| [손절 검토] | 보유 종목 수익률 ≤ `stop_loss_pct` |
| [목표가 도달] / [하한가 이탈] | 종목별 `sell_above` / `sell_below` |

같은 조건이 계속 유지되면 `cooldown_minutes`(기본 60분)마다만 다시 알립니다. 가격이 기준 밖으로 벗어나면 초기화되어, 다시 기준에 닿는 즉시 알립니다.

## 준비 1. 토스증권 API 키와 허용 IP

1. PC에서 토스증권 WTS에 로그인 → **설정 > Open API**에서 `client_id`, `client_secret`을 발급합니다.
2. 같은 화면 하단 **허용 IP 관리**에 봇을 실행할 컴퓨터의 **공인 IP**를 등록합니다.
   - 등록하지 않은 IP에서 호출하면 403으로 막힙니다.
   - 공인 IP가 자주 바뀌는 환경(일부 가정용 인터넷, 휴대폰 테더링)에서는 바뀔 때마다 다시 등록해야 합니다. 토스 FAQ도 고정 IP를 권장합니다. 계속 켜 둘 거라면 고정 IP가 있는 PC나 서버를 쓰세요.
3. **토큰은 클라이언트당 1개만 유효합니다.** 같은 키로 다른 프로그램(또는 봇 두 개)을 동시에 돌리면 서로 토큰을 무효화합니다. 봇은 잠금 파일로 자기 자신이 두 번 뜨는 것을 막습니다.

## 준비 2. 텔레그램 봇과 chat id

1. 텔레그램에서 **@BotFather** → `/newbot` → 이름을 정하면 **봇 토큰**(`123456789:AA...`)을 줍니다.
2. 만든 봇과의 대화창을 열고 아무 메시지(예: `/start`)를 보냅니다.
3. 브라우저에서 `https://api.telegram.org/bot<봇토큰>/getUpdates`를 열고, 결과의 `"chat":{"id":숫자}`가 **chat id**입니다.
   - 이 주소에는 봇 토큰이 들어 있으니 다른 사람에게 보내지 마세요.

## 설치와 설정

```bash
# 1) 비밀 값
cp .env.example .env          # Windows: copy .env.example .env
#    .env를 열어 TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 입력
#    (macOS/Linux) chmod 600 .env

# 2) 알림 규칙
cp config.example.toml config.toml
#    관심 종목, 매수 검토 가격, 익절·손절 % 를 본인 기준으로 수정
```

## 실행 순서 (처음엔 이 순서대로 확인)

```bash
python -m toss_alert --check-api        # 토스 연결, 계좌, 보유 종목, 관심 종목 시세가 보이는지
python -m toss_alert --test-telegram    # 텔레그램 테스트 메시지가 오는지
python -m toss_alert --dry-run --once   # 지금 조건에 걸리는 알림을 화면에만 출력
python -m toss_alert                    # 계속 실행 (Ctrl+C로 종료)
```

Windows에서 `python`이 안 되면 `py`로 바꿔서 실행하세요.

## 계속 켜 두기

- **Windows 작업 스케줄러:** "로그온할 때" 트리거로 `python -m toss_alert`(시작 위치: 이 폴더)를 등록합니다. 또는 1분마다 `python -m toss_alert --once`를 실행하게 해도 됩니다.
- **macOS/Linux:** `tmux`/`screen`에서 실행하거나 systemd 서비스로 등록합니다.
- 컴퓨터가 절전 모드에 들어가면 알림도 멈춥니다.

## 보안·이용 정책

- `.env`, `config.toml`, `alert_state.json`은 `.gitignore`에 들어 있습니다. 절대 공유하거나 git에 올리지 마세요.
- 키가 노출됐다면: 토스 WTS에서 Open API 키를 재발급하고, 텔레그램은 @BotFather의 `/revoke`로 봇 토큰을 바꾸세요.
- 로그와 오류 메시지에는 키·토큰을 남기지 않습니다(자동 가림 처리).
- 토스증권 데이터 이용 정책상 시세 데이터는 **본인 매매 목적으로만** 쓸 수 있고, 비상업적 용도라도 **제3자에게 배포하면 안 됩니다.** 알림은 본인 텔레그램으로만 받으세요(그룹방·채널 공유 금지).
- 이 알림은 투자 권유가 아닙니다. 시세는 지연되거나 틀릴 수 있으니 매매 전 앱에서 다시 확인하세요.

## 구성

```
toss_alert/
├── toss.py       # 토스 API: 토큰(401 시 1회 재발급), 계좌, 보유 종목, 현재가 (주문 API 없음)
├── http.py       # HTTPS 요청, 429 Retry-After·5xx 재시도(지수 백오프+지터), 오류 파싱
├── rules.py      # 알림 규칙 (순수 함수)
├── state.py      # 알림 중복 방지 상태 (원자적 저장)
├── telegram.py   # sendMessage, 평문 전송
├── config.py     # .env·config.toml 읽기와 검증
└── runner.py     # 1회 확인, 반복 실행, 단일 실행 잠금, CLI
tests/            # 가짜 HTTP 응답으로 동작 검증 (pytest)
```

API 근거: [토스증권 Open API 문서](https://developers.tossinvest.com/docs) (2026-10-29 확인). `POST /oauth2/token`, `GET /api/v1/accounts`, `GET /api/v1/holdings`(헤더 `X-Tossinvest-Account`), `GET /api/v1/prices?symbols=`(최대 200개).

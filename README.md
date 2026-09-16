# 세력의 매니저 (sepower-manager)

강회장 대신 텔레그램 채널을 운영하는 봇이다. 봇 계정은 @sepower_new_bot이고, 2026-09-17 라이브 영상 지시로 만들었다.
**PC가 꺼져 있어도 GitHub Actions에서 돈다.**

## 하는 일
1. **글감 수집** (30분마다) — 원천만 쓴다.
   - 구글뉴스 검색 19개: 가상자산 과세·클래리티·ETF·FOMC·엔비디아·HBM 등
   - 매체·공식 RSS: CoinDesk, The Block, Cointelegraph, 연준
   - 뉴스통신: 코인니스 / 데이터봇: 고래 입출금·대형 청산
   - 인플루언서 채널(에버그린·해달·박주혁 등)은 **글감으로 쓰지 않는다.** 문체만 분석해서 [STYLE.md](STYLE.md)로 정리했다.
2. **거르기** — 광고·이벤트·에어드랍, 쉴링, 채널끼리 서로 홍보하는 글, 중복 기사, 질 낮은 매체를 뺀다.
3. **고르기 + 쓰기** — `claude -p`가 한 번에 최대 2개를 고르고 STYLE.md 유형(A~F)으로 쓴다.
   - 여러 매체가 같이 다루는 이슈일수록 우선한다.
   - 음슴체, 짧게, 사진 + 한 줄 해석, 해시태그 1~2개
4. **이미지** — 기사 대표 사진 → 기사 화면 캡처 순으로 붙인다.
5. **고래/청산** — 1,000억 이상 고래 이동(거래소 내부 이동 제외), 100만 달러 이상 청산. 하루 3개/2개까지.
6. **아침 체크포인트** — 07:30 이후 첫 회차에 쓴다.
7. **승인 → 발행** — 초안이 강회장 DM으로 온다.
   - ✅ 올리기 / 🔁 다시쓰기 / 🗑 버리기
   - 초안에 답장으로 글을 보내면 그 글로 교체된다. `!더 짧게`처럼 보내면 그 지시대로 다시 쓴다.
   - `/auto`를 켜면 승인 없이 발행한다(하루 10개, 25분 간격).
   - 6시간 지난 초안은 자동으로 만료된다.

명령어: `/status` `/now` `/brief` `/pause` `/resume` `/auto` `/approve` `/help`

## 클라우드 구조
| 회차 | 주기 | 하는 일 | 시간 |
|---|---|---|---|
| `mode=updates` | 5분 | 버튼·명령 처리. 다시쓰기·/now·/brief가 있을 때만 claude 설치 | ~30초 |
| `mode=cycle` | 30분 | 버튼 처리 + 수집 → 초안 → DM/발행 | ~3분 |

- cron-job.org가 `workflow_dispatch`를 호출한다. GitHub `schedule`은 붐비면 건너뛰기 때문이다.
- 상태 DB는 `actions/cache`로 회차 간에 이어진다. 오래된 수집글은 3일 뒤 자동 삭제된다.
- Secrets:

  | 이름 | 내용 |
  |---|---|
  | `TELEGRAM_BOT_TOKEN_MANAGER` | 봇 토큰 |
  | `TELEGRAM_ADMIN_CHAT_ID` | 초안 받을 DM |
  | `TELEGRAM_TARGET_CHAT_ID` | 발행할 채널 |
  | `CLAUDE_CODE_OAUTH_TOKEN` | `claude setup-token` (구독, API 과금 없음) |

## 로컬
```
python run.py preview       # 전송 없이 초안 미리보기 → data/preview.html
python -m unittest discover tests
```
`start.vbs`(창 없는 상시 구동)는 클라우드와 **동시에 켜면 안 된다.** 버튼을 서로 가져간다.

## 문체 출처
`tools/parse_export.py`로 에버그린 대화 내보내기를 읽었고, 참고 채널 9곳의 최근 글을 분석했다.
분석은 길이·사진 비율·목록 비율·이모지·어미 기준이다. 원문은 저장소에 넣지 않았다(`data/`는 gitignore).

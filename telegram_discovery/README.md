# Telegram Discovery Scout

새로운 회사·산업·제품·병목·2차 수혜 구조를 발견하는 공개 Telegram 레이더입니다.
후보는 후속 AI 분석의 입력이며 투자 결론이나 공식 검증 결과가 아닙니다.
기존 `telegram/` monitor 및 `scripts/telegram_collect.py`는 수정하지 않습니다.
Discovery는 기존 모듈의 parser, offset-aware timestamp 처리, JSON merge만 import합니다.
Telegram 로그인·secret·봇 토큰·외부 Python 패키지는 사용하지 않습니다.

## 실행과 읽을 파일

- workflow: `.github/workflows/telegram-discovery.yml`
- 서울 06:20 / 12:20 / 18:20 = UTC 21:20 / 03:20 / 09:20.
- 수동 실행: GitHub Actions → Telegram Discovery Scout → Run workflow.
- 당일 누적 원문 게시물: `telegram_discovery/YYYY-MM-DD.json`
- 후속 AI 분석의 첫 입력: `telegram_discovery/candidates/YYYY-MM-DD.json`
- 30일 엔티티 통계·coverage·오류: `telegram_discovery/baseline/entities.json`
- 재현용 30일 게시물과 최초 관측 registry: `telegram_discovery/baseline/state.json`
- 운영 지침: `telegram_discovery/README.md`

후속 ChatGPT 예약 작업은 서울 날짜의 candidates, daily JSON, baseline entities,
그리고 이 지침을 읽습니다. candidates가 없거나 수집 오류가 있으면 성공한 부분과
빈 후보를 구분하고 보고합니다. 당일 급등 종목을 후보 부족의 대체재로 넣지 않습니다.

## Phase 1 채널

D1 (15): SK_Research_Asset, sk_smallcap, eqmirae, opendisco, hanasmallcap,
SKSCyclical, HI_GS, hana_us_stock, siglab, corevalue, growthresearch,
growth_semi, knowledge_to_wealth, sksresearch, HanaResearch.

D2 (4): quantum_algo, STANDARDCAPITAL, FastStockNewsUSA, FastStockNews.
D2는 발견 레이더입니다. D2만 있는 신호는 항상 `radar_only`이며 최종 근거로 쓰지 않습니다.

## 수집·누적 원칙

게시 시각은 HTML `<time datetime>`만 사용합니다. UTC/offset이 없는 시각을 거부하고
`zoneinfo.ZoneInfo("Asia/Seoul")`로 변환합니다. 본문 날짜는 게시 날짜 추정에 사용하지 않습니다.
각 채널은 독립적으로 요청하며 4개 worker를 사용합니다. HTTP/파싱/페이지 예산 오류는
채널명·URL·원인과 함께 errors와 Actions warning/log에 남깁니다. 다른 채널은 계속 수집합니다.
HTML 원문은 저장하지 않으며 공개 게시물의 전체 body와 permalink만 저장합니다.

당일 JSON은 `(channel, message_id)`로 누적 병합합니다. Telegram handle 대소문자는
동일하게 취급합니다. 기존 body·최초 수집시각은 보존하고 새 ID만 추가합니다.
모든 posts는 offset-aware `published_at_kst` 오름차순입니다. errors·channels·최상위 수집시각은
최신 실행으로 교체합니다. 저장은 임시 파일 후 atomic replace이며 잘못된 baseline JSON은
조용히 초기화하지 않고 실행을 실패시켜 보존합니다.

## 30일 baseline과 불완전 이력

첫 실행은 공개 preview의 `?before=<message_id>`를 사용해 최근 30일을 역방향 수집합니다.
각 채널 최대 40페이지·100초로 제한합니다. 속보 채널, 삭제된 게시물, preview 제한 때문에
30일 모두 복구하지 못할 수 있습니다. `coverage.<channel>`의 history_complete,
earliest_observed_at_kst, latest_run_complete, errors_count로 실제 범위를 표시합니다.
완료되지 않은 채널은 다음 실행에서 최신 72시간을 수집하고, 저장한
backfill_next_before 커서부터 과거 bootstrap을 이어갑니다. 동일한 첫 페이지들만
계속 재시도하지 않습니다. 과거 커서가 없으면 30일 bootstrap을 재시도합니다.

완료된 채널은 최소 72시간 겹쳐 읽고 마지막 성공 이후 누락 구간까지 범위를 넓힙니다.
state posts는 실행 시각에서 30일 이전을 제거하는 rolling window입니다.
각 엔티티의 first_seen은 시스템이 관측한 가장 이른 게시 시각을 registry에 보존하며,
이는 Telegram 전체 역사상의 최초 등장을 뜻하지 않습니다. last_seen·mention_count·channel_count·
mentions_72h·prior_mentions·permalinks를 재현 가능하게 계산합니다.
prior_mentions는 현재 30일 창에서 최근 72시간을 제외한 구간의 게시물 수입니다.

모든 19채널의 요청 구간이 확인된 때만 baseline_status=complete입니다.
그 외는 partial이며 후보의 novelty는 unconfirmed입니다. 최초 실행의 신규성이나
불완전 이력에서의 0회 언급을 “최근 30일에 없었던 새 기업”이라고 단정하지 않습니다.
수집된 이력과 seed/regex extractor에서 찾지 못한 언급도 존재할 수 있습니다.

## 엔티티·후보 생성 규칙

seed 별칭 외에도 종목코드, `$TICKER` / 거래소 ticker, 한글 리포트 제목, hashtag,
사업 변화 문맥의 이름, 영어 회사/제품/기술 이름, 프로젝트명을 추출합니다.
따라서 HBM·DRAM·AI 키워드 목록으로 한정되지 않습니다. 한글 일반명사 오인,
회사·고객·공급사 역할 구분 누락은 AI 분석에서 수정해야 합니다. entity category는 휴리스틱입니다.

엔티티 주변의 수주·CAPA·가동률·수출·backlog·lead time·채택·계약·가격·믹스·마진 문맥에서
실물 숫자를 찾고 Q/P/mix/margin 경로 태그를 붙입니다. 같은 원문의 SHA256,
near-copy SimHash, 같은 원기사/리포트 URL을 공유하는 게시물은 하나의 source cluster로 묶습니다.
독립 채널 수는 channel와 source cluster가 각각 중복되지 않게 maximum matching으로 셉니다.
이는 출처 독립성의 근사치입니다. URL 없는 재작성·스크린샷·서로 다른 기사에서 인용한 같은
원리포트는 완벽하게 제거할 수 없으므로 후속 분석에서 원자료를 재확인해야 합니다.

각 후보에는 entity, category, first_seen, mentions_72h, independent_channels,
evidence_posts, why_unusual이 있습니다. 낮은 prior_mentions 또는 관측된 언급 급증과
엔티티 근처 사업 숫자가 있는 경우만 후보군에 남깁니다.
2개 이상 독립적인 숫자 근거와 D1 자료가 있으면 follow_up, 그 외는 radar_only입니다.
빈 후보도 정상입니다. mentions_72h는 재전송 포함 게시물 수이고 independent_channels는
재전송을 제거한 숫자입니다. business_independent_channels는 숫자 근거가 있는 독립 채널 수입니다.
단순 목표가/급등/테마 나열에는 사업 숫자가 없으므로 제외합니다. 루머·VIP·홍보 문구는
제외하고 목표가 등 잔여 잡음은 noise_penalty로 감점합니다.
공식자료 검증·시장 미반영 여부는 프로그램이 확정하지 않습니다.

## 후속 AI 분석 A–G

A. 무엇이 새로 변했는가: 과거와 현재의 수량·가격·리드타임·가동률을 비교합니다.
B. 누가 돈을 버는가: 회사·고객사·공급업체·2차 수혜 기업과 경쟁자를 식별합니다.
C. Q/P/mix/margin 중 어느 경로인가: 실적 수치로 연결되는 과정을 설명합니다.
D. 일시적 뉴스인가 구조적 변화인가: 반복 주문·증설·채택·공급 제약의 지속성을 확인합니다.
E. 시장이 아직 충분히 반영하지 않았는가: 컨센서스·가이던스·추정 변화와 대조합니다.
F. Telegram 외 공식 자료로 검증 가능한가: 공시·IR·실적발표·공식 통계·고객 발표를 확인합니다.
G. 틀렸다고 판단할 조건은 무엇인가: 숫자로 확인할 반증과 다음 점검 시점을 정합니다.

같은 회사의 언급이라도 전문가·증권사가 서로 다른 사업상 이유로 언급하는지 확인합니다.
단순 목표가 상향, 당일 급등, “AI 관련주” 나열, 무근거 루머, 소형주 홍보, 숫자나
사업 변화가 없는 의견, 원기사·리포트 재전송은 강하게 감점하거나 제외합니다.
Telegram은 발견 근거로 인용하고 최종 투자 근거는 공식 자료의 원문 URL·게시일·수치로 남깁니다.

## 최종 아이디어 카드

- 후보 기업/산업
- 왜 지금 발견됐는가
- Telegram에서 포착된 신호 (시간·permalink·baseline coverage 포함)
- 독립 채널 수 (원자료 중복 및 서로 다른 언급 이유 확인)
- 공식자료로 확인된 사실 (공식 URL·기준일·수치; 미검증이면 명시)
- 이익 발생 경로 (Q/P/mix/margin)
- 시장 미반영 가능성 (검증한 컨센서스와 추정; 근거 없으면 보류)
- 반론
- Thesis가 깨지는 조건
- 다음 확인 데이터

## 검증

`python3 -m unittest discover -s tests -p 'test_telegram_discovery.py' -v`
실물 수집: `python3 scripts/telegram_discovery.py`.
테스트는 timestamp, 신규 이름, 재전송, cold start, 잡음 제외, D2 제한,
두 번 실행의 merge/정렬/상태 갱신, baseline expiry/최초 관측 보존, cron·저장 경계를 검사합니다.

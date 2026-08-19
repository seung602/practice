# Olive Young Beauty Trend Database

올리브영 상품 마스터 + 상품 변화 이력 + 판매 랭킹을 SQLite에 누적하는 GitHub Actions 프로젝트입니다.

## 중요

이 프로젝트는 올리브영의 공개 웹페이지에서 확인 가능한 목록 페이지를 기반으로 동작합니다.
올리브영이 공개적으로 문서화하지 않은 내부 API endpoint를 임의로 만들어 사용하지 않습니다.

현재 공개 페이지에서 랭킹 카테고리와 상품 목록의 24/36/48개 보기, 상품 수 등의 구조가 확인되지만,
사이트의 HTML/페이지네이션이 바뀌면 collector가 실패할 수 있습니다.
따라서 수집량 검증을 통과하지 못하면 기존 상품을 INACTIVE로 바꾸지 않습니다.

## 파일

- `run_daily.py` : 전체 실행
- `catalog_collector.py` : 전체 상품 수집
- `ranking_collector.py` : Top 30 판매 랭킹
- `oliveyoung_client.py` : HTTP/페이지네이션
- `oliveyoung_parser.py` : HTML → 상품 데이터
- `db.py` : SQLite schema/migration/upsert
- `config.py` : 수집 설정
- `.github/workflows/daily_collector.yml` : 매일 09:00 KST 실행

## DB

### products
현재 상품 마스터.

### product_snapshots
날짜별 상품 정보 스냅샷.

### daily_rankings
날짜별 랭킹 원본.

### ranking_changes
전일 대비 상승/하락/신규/재진입.

### catalog_runs
전체상품 수집 성공/실패 기록.

## 카탈로그 수집 안정성

이전에는 페이지 요청 실패 시 `catalog_collector.py`에서 3회, `oliveyoung_client.py`
내부에서 또 3회 재시도해서 최악의 경우 한 페이지당 9번까지 브라우저를 열고
5분 이상 낭비하는 문제가 있었습니다. 지금은 재시도 책임을 client 쪽 한 곳으로
일원화했습니다:

- 네트워크/HTTP 오류: client가 최대 3회, 지수 백오프(8초→20초→50초)로 재시도
- 상품 리스트 셀렉터(`.prd_list`, `.cate_prd_list`) 미출현: 카테고리의 실제
  마지막 페이지(상품이 더 없음)일 가능성이 높으므로, 가볍게 1번만 재확인하고
  더 이상 재시도하지 않습니다. 실제로 상품이 있는지는 `parse_products()`가
  최종 판단하며, 0개면 "빈 페이지 2회 연속 → 카테고리 종료" 규칙으로 자연스럽게
  넘어갑니다.

## 상품 상태

- ACTIVE: 최근 전체상품 목록에서 확인
- MISSING: 최근 1~6일 미확인
- SUSPECTED_INACTIVE: 7일 이상 미확인 (`MISSING_DAYS_TO_SUSPECT`)
- INACTIVE: 30일 이상 미확인 (`MISSING_DAYS_TO_INACTIVE`)

정확한 단종 여부를 의미하지 않습니다.

상태 전이는 카탈로그 수집 성공 시 `db.update_missing_status_transitions()`가
`suspected_missing_since` 기준 경과일수를 계산해서 자동으로 처리합니다
(카탈로그에 다시 나타나면 `upsert_product`가 즉시 ACTIVE로 복귀시킵니다).

## 운영 관련 필수 설정

### GitHub Secrets

- `RENDER_DEPLOY_HOOK_URL`: Render 배포 훅 전체 URL(키 포함)을 여기에 등록하세요.
  (Settings → Secrets and variables → Actions → New repository secret)
  등록하지 않으면 배포 트리거 단계는 건너뜁니다.

### Git LFS

`beauty_catalog.db`는 매일 갱신되는 바이너리 파일이라 `.gitattributes`로 Git LFS 추적을
설정해두었습니다. 로컬에서 처음 클론/작업할 때는 `git lfs install`을 한 번 실행해야 합니다.
GitHub Actions에서는 워크플로우의 `actions/checkout@v4`에 `lfs: true`가 설정되어 있어
별도 조치가 필요 없습니다. (단, GitHub 무료 플랜의 LFS 저장/대역폭 할당량(1GB/월)을
넘으면 추가 결제가 필요할 수 있으니 주기적으로 사용량을 확인하세요.)

## 첫 테스트

로컬:

```bash
pip install -r requirements.txt
python run_daily.py
```

GitHub:

1. 새 Repository 생성
2. 이 프로젝트 파일 전체 업로드
3. Actions → Daily Olive Young Beauty Data
4. Run workflow

## 수집량 안전장치

`MIN_CATALOG_ITEMS`보다 적은 상품이 수집되면 실행을 실패시킵니다.

이 장치는 HTML 변경, 차단, 일시적인 오류 때문에 "전체 상품이 전부 사라졌다"고 오판하는 것을 막습니다.

기본값:

```text
MIN_CATALOG_ITEMS=100
ROWS_PER_PAGE=48
MAX_PAGES_PER_SURFACE=1000
REQUEST_DELAY_SECONDS=1.0
```

장기 운영 전에 Actions 첫 실행 로그에서 실제 unique product 수와 page 수를 확인하세요.

## 향후 확장

- 올리브영 카테고리별 랭킹
- 7/14/30일 상승 모멘텀
- 신규상품 → Top30 진입까지 걸린 시간
- 재진입/급상승 탐지
- 브랜드별 동시 상승
- 키워드/성분/제품군 트렌드
- Telegram trend report

올리브영 수집기 최종 검증/수정본 (2026-09-25)

검증 완료:
1) Python 전체 파일 py_compile 통과
2) oliveyoung_parser_test.py: 100개 상품 회귀 테스트 통과
3) 100개 연속 rank 1~100 검증 통과
4) 현재 올리브영 공개 랭킹 페이지에서 100위까지 실제 상품이 노출되는 것을 웹으로 확인
5) 현재 상품 상세 링크가 /store/goods/getGoodsDetail.do?goodsNo=... 형태임을 확인
6) GitHub Actions workflow에 Playwright 설치/정적 테스트/live smoke test/pipefail/diagnostic artifact 포함
7) smoke test가 하드코딩된 특정 카테고리 코드가 아니라 config의 실제 SUBCATEGORIES 첫 항목을 사용하도록 수정

핵심 수정:
- 구형 Chrome 124 고정 UA 제거
- Playwright 설치 Chromium의 정상 UA 사용
- 세션 prime + 컨텍스트 재생성 + HTTP 401/403/429/451/503 감지
- challenge/block HTML 감지 및 진단 HTML 저장
- 상품 상세 링크(goodsNo/gdsNo) 중심의 범용 파싱
- 구형 .prd_list/.cate_prd_list 클래스에만 의존하지 않음
- 랭킹 번호 링크를 상품명으로 오인하지 않도록 numeric-only name 제거
- 랭킹 결과 80개 미만 / 중복 / 비연속 rank 안전장치
- 카탈로그 수집 실패 시 PARTIAL_PAGE_FAILURE로 기록하고 전체 실행 실패 처리
- GitHub Actions의 `python run_daily.py | tee`에 `set -o pipefail` 적용
- smoke test 실패 시 전체 workflow 중단
- Olive Young diagnostic HTML과 run_log를 Actions artifact로 보존

중요:
이 개발 환경에서는 외부 DNS 제한 때문에 Playwright Chromium 자체를 다운로드/실행하여 Olive Young에 직접 접속하는 마지막 live smoke 실행은 수행할 수 없었습니다. 대신 현재 공개 Olive Young 페이지를 웹으로 확인했고, 로컬 코드/회귀 테스트는 통과했습니다. 실제 GitHub Actions Ubuntu runner에서 `Olive Young live smoke test`가 마지막 실환경 검증입니다.

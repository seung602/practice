import os

# ---- 다이소몰 SearchGoods API ----
DAISO_BASE_URL = "https://www.daisomall.co.kr"
DAISO_SEARCH_URL = f"{DAISO_BASE_URL}/ssn/search/SearchGoods"

# ⚠️ 이전 버전에서는 이 딕셔너리의 key/value에 trailing space가 섞여 있어서
# exhCtgrNo가 실제 카테고리 코드와 일치하지 않는 문제가 있었음(예: "CTGR_01051 " != "CTGR_01051").
# 그 결과 서버가 카테고리를 못 찾고 무관한 추천/트렌딩 결과를 반환했을 가능성이 있음.
# 아래 값들은 전부 공백 없이 정리됨.
DAISO_SEARCH_STATIC_PARAMS = {
    "searchTerm": "",
    "searchQuery": "",
    "brndCd": "",
    "userId": "",
    "newPdYn": "",
    "massOrPsblYn": "",
    "pkupOrPsblYn": "",
    "fdrmOrPsblYn": "",
    "quickOrPsblYn": "",
    "searchSort": "",
    "isCategory": "1",
    "mallId": "MALL_MAIN",
}
DAISO_ROWS_PER_PAGE = int(os.getenv("DAISO_ROWS_PER_PAGE", "30"))
DAISO_MAX_PAGES_PER_CATEGORY = int(os.getenv("DAISO_MAX_PAGES_PER_CATEGORY", "500"))

# 뷰티/위생(CTGR_01050) 대카테고리
DAISO_LARGE_CTGR_NO = "CTGR_01050"

# ---- 화장품 집중 카테고리 ----
# 면봉·위생용품류(뷰티소품), 네일, 헤어/바디 등 기타용품은 제외하여
# 랭킹이 화장품 위주로 구성되도록 합니다.
DAISO_CATEGORIES = [
    ("스킨케어", "CTGR_01051"),
    ("마스크팩", "CTGR_01052"),
    ("클렌징", "CTGR_01053"),
    ("선케어", "CTGR_01054"),
    ("메이크업", "CTGR_01055"),
    ("맨즈케어", "CTGR_01058"),
    ("향수", "CTGR_01059"),
]

# ---- 신상 수집 ----
# 다이소몰의 '신상'은 독립 CTGR 코드가 아닌 newPdYn=Y 필터 형태입니다.
# 수집 카테고리 내 신상 상품은 응답의 newPdYn 필드로 자동 판별되어
# is_new=Y 로 DB에 저장되고, 웹 대시보드에 🆕 신상으로 표시됩니다.
DAISO_NEW_PDN = "Y"

# referer 헤더용 (뷰티 대카테고리 공통 페이지 코드)
DAISO_EXH_PAGE_CODE = "C208"


def daiso_referer(middle_ctgr_no):
    return f"{DAISO_BASE_URL}/ds/exhCtgr/{DAISO_EXH_PAGE_CODE}/{DAISO_LARGE_CTGR_NO}/{middle_ctgr_no}"


# 실제 다이소몰 상품 페이지 주소 (PDP가 아니라 PDR!)
DAISO_PRODUCT_URL_TEMPLATE = DAISO_BASE_URL + "/pd/pdr/SCR_PDR_0001?pdNo={pd_no}"

DAISO_USER_AGENT = os.getenv(
    "DAISO_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# 카탈로그 수집량 안전장치
# ⚠️ 카테고리 축소에 따라 수집량이 줄어들므로 1000 → 100 으로 하향
# (너무 높으면 FAILED_MIN_ITEMS 로 전체 수집이 무시됩니다)
MIN_DAISO_CATALOG_ITEMS = 100

DAISO_REQUEST_TIMEOUT = int(os.getenv("DAISO_REQUEST_TIMEOUT", "20"))
DAISO_REQUEST_DELAY_SECONDS = float(os.getenv("DAISO_REQUEST_DELAY_SECONDS", "0.8"))
DAISO_MAX_RETRIES = 3
DAISO_BASE_RETRY_DELAY_SECONDS = 5
DAISO_RETRY_BACKOFF_FACTOR = 2.5
DAISO_RETRY_JITTER_SECONDS = 3

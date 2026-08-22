import os
import json

DB_PATH = os.getenv("DB_PATH", "beauty_catalog.db")
BASE_URL = "https://www.oliveyoung.co.kr"
MAIN_URL = f"{BASE_URL}/store/main/main.do?oy=0"
RANKING_URL = f"{BASE_URL}/store/main/getBestList.do"

ROWS_PER_PAGE = int(os.getenv("ROWS_PER_PAGE", "48"))
MAX_PAGES_PER_SURFACE = int(os.getenv("MAX_PAGES_PER_SURFACE", "1000"))
MIN_CATALOG_ITEMS = int(os.getenv("MIN_CATALOG_ITEMS", "100"))

MISSING_DAYS_TO_SUSPECT = int(os.getenv("MISSING_DAYS_TO_SUSPECT", "7"))
MISSING_DAYS_TO_INACTIVE = int(os.getenv("MISSING_DAYS_TO_INACTIVE", "30"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
# 페이지 요청 간 기본 지연 시간 (초). 실제 적용 시 지터(Jitter)가 추가됨
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "1.5"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_URL + "/",
}

# 실패한 페이지 기록 파일 경로
FAILED_PAGES_FILE = "failed_pages.json"

RANKING_CATEGORIES = [
    ("ALL", ""),
    ("스킨케어", "10000010001"),
    ("마스크팩", "10000010009"),
    ("클렌징", "10000010010"),
    ("선케어", "10000010011"),
    ("뷰티소품", "10000010006"),
    ("더모 코스메틱", "10000010008"),
    ("헤어케어", "10000010004"),
    ("바디케어", "10000010003"),
]

SUBCATEGORIES = {
    "스킨케어": [
        ("스킨/토너", "100000100010013"),
        ("에센스/세럼/앰플", "100000100010014"),
        ("크림", "100000100010015"),
        ("로션", "100000100010016"),
        ("미스트/오일", "100000100010010"),
        ("스킨케어세트", "100000100010017"),
        ("스킨케어 디바이스", "100000100010018"),
    ],
    "마스크팩": [
        ("시트팩", "100000100090001"),
        ("패드", "100000100090004"),
        ("페이셜팩", "100000100090002"),
        ("코팩", "100000100090005"),
        ("패치", "100000100090006"),
    ],
    "클렌징": [
        ("클렌징폼/젤", "100000100100001"),
        ("오일/밤", "100000100100004"),
        ("워터/밀크", "100000100100005"),
        ("립 &아이리무버", "100000100100006"),
        ("필링 &스크럽", "100000100100007"),
        ("티슈/패드", "100000100100008"),
        ("클렌징 디바이스", "100000100100009"),
    ],
    "선케어": [
        ("태닝/애프터선", "100000100110002"),
        ("선크림", "100000100110006"),
        ("선스틱", "100000100110003"),
        ("선쿠션", "100000100110004"),
        ("선스프레이/선패치", "100000100110005"),
    ],
    "뷰티소품": [
        ("메이크업 툴", "100000100060001"),
        ("헤어/바디 툴", "100000100060002"),
        ("데일리 툴", "100000100060005"),
        ("페이스 툴", "100000100060006"),
        ("아이래쉬 툴", "100000100060007"),
    ],
    "더모 코스메틱": [
        ("바디케어", "100000100080004"),
        ("선케어", "100000100080005"),
        ("클렌징", "100000100080006"),
        ("마스크팩", "100000100080011"),
    ],
    "헤어케어": [
        ("트리트먼트/팩", "100000100040007"),
        ("헤어기기/브러시", "100000100040004"),
        ("염모제/펌", "100000100040010"),
        ("스타일링", "100000100040011"),
        ("헤어에센스", "100000100040013"),
        ("두피에센스", "100000100040014"),
    ],
    "바디케어": [
        ("샤워/입욕", "100000100030005"),
        ("데오드란트", "100000100030012"),
        ("핸드케어", "100000100030016"),
        ("제모/왁싱", "100000100030019"),
        ("유아동/임산부", "100000100030020"),
        ("오일/미스트", "100000100030022"),
        ("풋케어", "100000100030024"),
        ("바디로션/크림", "100000100030025"),
    ],
}

PROBE_CATEGORIES = []

# --- 상품명 영어 번역(Gemini) 설정 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # 고용량/저비용 모델
TRANSLATE_ENABLED = os.getenv("TRANSLATE_ENABLED", "1") == "1"
TRANSLATE_BATCH_SIZE = int(os.getenv("TRANSLATE_BATCH_SIZE", "40"))
# 하루 실행당 번역 호출 상한(신규/변경 상품이 폭증해도 API 비용이 튀지 않도록 하는 안전장치).
# 못 채운 나머지는 다음 날 실행에서 이어서 처리됨(캐시 방식이라 유실되지 않음).
TRANSLATE_MAX_PER_RUN = int(os.getenv("TRANSLATE_MAX_PER_RUN", "1500"))

# 실패 페이지 로드/저장 헬퍼 함수
def load_failed_pages():
    if os.path.exists(FAILED_PAGES_FILE):
        try:
            with open(FAILED_PAGES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_failed_pages(failed_pages_dict):
    try:
        with open(FAILED_PAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(failed_pages_dict, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"실패 페이지 저장 실패: {e}")

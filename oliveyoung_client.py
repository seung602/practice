import logging
import random
import re
import time

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 8
RETRY_BACKOFF_FACTOR = 2.5
RETRY_JITTER_SECONDS = 4

# 셀렉터 대기 (카테고리 끝 페이지 빠른 감지용)
SELECTOR_TIMEOUT_MS = 800
# 셀렉터 미출현 시 재확인 전 대기
SELECTOR_RECHECK_SLEEP = (0.2, 0.4)


def _retry_delay(attempt):
    delay = BASE_RETRY_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    return delay + random.uniform(0, RETRY_JITTER_SECONDS)


class OliveYoungClient:
    """
    - 브라우저는 생명주기 동안 1번만 띄움
    - 페이지(context)도 1개만 재사용 → 매 요청마다 새로 만드는 오버헤드 제거
    - 이미지/CSS/폰트 차단으로 로딩 속도 향상
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        if self._browser is not None:
            return

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1920, "height": 1080},
            locale="ko-KR",
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # 이미지 / 스타일시트 / 폰트 / 미디어 차단 → 로딩 대폭 단축
        self._context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "stylesheet", "font", "media")
                else route.continue_()
            ),
        )

        self._page = self._context.new_page()

    def close(self):
        for obj, closer in [
            (self._page, "close"),
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ]:
            if obj is not None:
                try:
                    getattr(obj, closer)()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _fetch_once(self, url, wait_selector=None):
        self.start()
        page = self._page

        response = page.goto(url, wait_until="domcontentloaded", timeout=25000)

        if response is not None and response.status == 403:
            raise Exception(f"HTTP Error 403 (차단됨): {url}")

        selector_found = True
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=SELECTOR_TIMEOUT_MS)
            except Exception:
                selector_found = False

        html = page.content()
        return html, selector_found

    def _fetch_with_browser(self, url, wait_selector=None):
        """
        네트워크/HTTP 오류: MAX_RETRIES(3회)까지 지수 백오프 재시도.
        셀렉터 미출현: 1번만 가볍게 재확인 후 파서 판단에 위임.
        """
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                html, selector_found = self._fetch_once(url, wait_selector)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        f"요청 실패({attempt}/{MAX_RETRIES}), {delay:.1f}초 후 재시도: {e}"
                    )
                    time.sleep(delay)
                continue

            if wait_selector and not selector_found:
                if attempt == 1:
                    logger.info(
                        f"selector 미출현 - 짧게 한 번만 재확인 후 계속 진행: "
                        f"{wait_selector} ({url})"
                    )
                    time.sleep(random.uniform(*SELECTOR_RECHECK_SLEEP))
                    continue
                else:
                    logger.info(
                        f"selector 재확인 후에도 미출현 - 카테고리 종료 페이지일 수 있어 "
                        f"파서 판단에 위임: {wait_selector} ({url})"
                    )
                    return html

            return html

        raise last_error or Exception(f"요청 실패: {url}")

    def discover_subcategories(self, parent_disp_cat_no):
        url = (
            "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do"
            f"?dispCatNo={parent_disp_cat_no}"
        )

        self.start()
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        try:
            page.wait_for_selector('a[href*="dispCatNo="]', timeout=5000)
        except Exception:
            pass
        time.sleep(0.8)
        html = page.content()

        soup = BeautifulSoup(html, "html.parser")
        subcategories = []
        seen = set()

        for link in soup.find_all("a", href=True):
            m = re.search(r"dispCatNo=(\d+)", link.get("href", ""))
            if not m:
                continue

            code = m.group(1)
            if code == parent_disp_cat_no:
                continue
            if not code.startswith(parent_disp_cat_no):
                continue
            if code in seen:
                continue

            seen.add(code)
            name = link.get_text(strip=True) or f"sub_{code[-4:]}"
            subcategories.append({"name": name, "disp_cat_no": code})

        logger.info(f"[{parent_disp_cat_no}] 세부카테고리 발견: {len(subcategories)}개")
        return subcategories

    def fetch_top100(self):
        url = (
            "https://www.oliveyoung.co.kr/store/main/getBestList.do"
            "?t_page=%ED%99%88&t_click=GNB&t_gnb_type=%EB%9E%AD%ED%82%B9&t_swiping_type=N"
        )
        return self._fetch_with_browser(
            url,
            wait_selector=".cate_prd_list > li",
        )

    def fetch_category_page(self, disp_cat_no, page_idx=1, rows_per_page=48):
        if len(disp_cat_no) > 11:
            base = "https://www.oliveyoung.co.kr/store/display/getMCategoryList.do"
        else:
            base = "https://www.oliveyoung.co.kr/store/display/getCategoryShop.do"

        url = (
            f"{base}"
            f"?dispCatNo={disp_cat_no}"
            f"&pageIdx={page_idx}"
            f"&rowsPerPage={rows_per_page}"
            "&prdSort=01"
        )
        return self._fetch_with_browser(url, wait_selector=".prd_list, .cate_prd_list")

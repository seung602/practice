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


def _retry_delay(attempt):
    delay = BASE_RETRY_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    return delay + random.uniform(0, RETRY_JITTER_SECONDS)


class OliveYoungClient:
    """
    ⚠️ 이전 버전은 fetch 1번마다 sync_playwright() ~ browser.launch() ~ browser.close()를
    새로 반복해서, 카테고리/페이지가 많아질수록 브라우저 기동 오버헤드가 누적되어
    전체 수집 시간이 크게 늘어나는 문제가 있었다.

    이제는 Chromium 브라우저를 클라이언트 생명주기 동안 1번만 띄워두고,
    요청마다 가벼운 BrowserContext/Page만 새로 만들어 재사용한다.
    (컨텍스트를 요청마다 새로 만드는 이유: 세션/쿠키가 섞여 차단 감지 위험이
    커지는 것을 막기 위함. 브라우저 프로세스 자체만 재사용해도 대부분의 오버헤드가 사라진다.)

    사용법:
        with OliveYoungClient() as client:
            html = client.fetch_top30()
            ...

    또는 명시적으로 close() 호출:
        client = OliveYoungClient()
        try:
            ...
        finally:
            client.close()
    """

    def __init__(self):
        self._playwright = None
        self._browser = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)

    def close(self):
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _new_page(self):
        self.start()
        context = self._browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1920, "height": 1080},
            locale="ko-KR",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return context, context.new_page()

    def _fetch_once(self, url, wait_selector=None):
        context, page = self._new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)

            if response is not None and response.status == 403:
                raise Exception(f"HTTP Error 403 (차단됨): {url}")

            selector_found = True
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15000)
                except Exception:
                    selector_found = False

            html = page.content()
            return html, selector_found
        finally:
            context.close()

    def _fetch_with_browser(self, url, wait_selector=None):
        """
        네트워크/HTTP 오류: MAX_RETRIES(3회)까지 지수 백오프로 재시도 (실제 장애 대응용).

        ⚠️ 셀렉터 미출현(wait_for_selector timeout)은 "차단"일 수도 있지만,
        카테고리의 마지막 페이지처럼 실제로 상품이 없어서 사이트가 다른 형태의
        페이지를 돌려주는 정상적인 경우일 수도 있다. 이걸 구분할 방법이 없으므로,
        예전처럼 무겁게(최대 3회, 회당 최대 15초+백오프) 재시도해서 예외를 던지는 대신
        가볍게 딱 1번만 더 확인해보고, 그래도 안 뜨면 지금까지 받은 HTML을 그대로
        반환한다. 실제로 상품이 있는지 없는지는 parse_products()가 최종 판단하므로,
        진짜 빈 페이지면 자연스럽게 "카테고리 종료"로 처리되고, 진짜 차단이었다면
        parse_products()도 0개를 반환해 동일하게 처리된다(과도한 재시도로 시간을
        낭비하지 않음).
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
                    # 렌더링 지연일 가능성에 대비해 짧게 한 번만 재확인
                    logger.info(
                        f"selector 미출현 - 짧게 한 번만 재확인 후 계속 진행: "
                        f"{wait_selector} ({url})"
                    )
                    time.sleep(3 + random.uniform(0, 2))
                    continue
                else:
                    # 더 이상 재시도하지 않고 지금까지 받은 HTML을 그대로 반환.
                    # (카테고리 종료 페이지인지 실제 차단인지는 parse_products()가 판단)
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

        context, page = self._new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector('a[href*="dispCatNo="]', timeout=10000)
            except Exception:
                pass
            time.sleep(2)
            html = page.content()
        finally:
            context.close()

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

    def fetch_top30(self):
        return self._fetch_with_browser(
            "https://www.oliveyoung.co.kr/store/main/getBestList.do",
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

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
# ⚠️ 800ms는 너무 짧아서 서버가 살짝 느린 순간 정상 페이지를 "빈 페이지"로
# 오인할 위험이 있음 (조용히 뒷페이지 누락 -> status=SUCCESS로 찍힘).
# 속도 이득 대부분은 컨텍스트 재사용 + 에셋 차단에서 이미 나오므로,
# 여기는 안전 마진을 조금 더 준다.
SELECTOR_TIMEOUT_MS = 1100
# 셀렉터 미출현 시 재확인 전 대기
SELECTOR_RECHECK_SLEEP = (0.3, 0.5)

# 페이지/컨텍스트를 이 횟수만큼 쓰면 선제적으로 재생성 (메모리 누적/세션 노후화 방지)
RECYCLE_AFTER_REQUESTS = 300


def _retry_delay(attempt):
    delay = BASE_RETRY_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    return delay + random.uniform(0, RETRY_JITTER_SECONDS)


class OliveYoungClient:
    """
    - 브라우저는 생명주기 동안 1번만 띄움
    - 페이지(context)도 기본적으로 재사용 -> 매 요청마다 새로 만드는 오버헤드 제거
    - 이미지/CSS/폰트 차단으로 로딩 속도 향상
    - 🚨 추가: 페이지/컨텍스트가 죽은 것으로 판단되면 자동으로 재생성해서
      "한 번 크래시하면 남은 수백~수천 페이지가 전부 재시도-백오프를 반복하며
      시간을 낭비하는" 문제를 방지한다.
    - 🚨 추가: RECYCLE_AFTER_REQUESTS 요청마다 컨텍스트를 선제적으로 새로 만들어
      장시간 실행 시 메모리 누적/세션 노후화를 예방한다.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._request_count = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ── 브라우저/컨텍스트 생명주기 ──────────────────────────────────────

    def start(self):
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        if self._context is None or self._page is None:
            self._new_context()

    def _new_context(self):
        """기존 컨텍스트/페이지를 정리하고 새로 만든다."""
        self._close_context_only()

        self._context = self._browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1920, "height": 1080},
            locale="ko-KR",
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "stylesheet", "font", "media")
                else route.continue_()
            ),
        )
        self._page = self._context.new_page()
        self._request_count = 0

    def _close_context_only(self):
        for obj, closer in [(self._page, "close"), (self._context, "close")]:
            if obj is not None:
                try:
                    getattr(obj, closer)()
                except Exception:
                    pass
        self._page = None
        self._context = None

    def close(self):
        self._close_context_only()
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

    def _page_is_healthy(self):
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    # ── 요청 ────────────────────────────────────────────────────────────

    def _fetch_once(self, url, wait_selector=None):
        self.start()

        # 선제적 재생성: 너무 오래 같은 페이지를 재사용하지 않도록
        if self._request_count >= RECYCLE_AFTER_REQUESTS:
            logger.info(f"컨텍스트 {RECYCLE_AFTER_REQUESTS}회 사용 - 선제적으로 재생성합니다.")
            self._new_context()

        # 크래시 감지: 죽어있으면 재생성 후 진행
        if not self._page_is_healthy():
            logger.warning("페이지가 비정상 상태로 감지되어 컨텍스트를 재생성합니다.")
            self._new_context()

        page = self._page
        self._request_count += 1

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

        🚨 재시도 전에는 항상 페이지 상태를 점검해서, 크래시로 인한 연쇄 실패를
        방지한다 (죽은 페이지로 계속 재시도하며 시간만 낭비하는 것을 막음).
        """
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                html, selector_found = self._fetch_once(url, wait_selector)
            except Exception as e:
                last_error = e
                logger.warning(f"요청 실패({attempt}/{MAX_RETRIES}): {e}")

                # 크래시성 오류로 의심되면 즉시 컨텍스트 재생성 (다음 시도부터 정상 페이지로)
                if not self._page_is_healthy():
                    logger.warning("크래시 의심 - 컨텍스트를 재생성한 뒤 재시도합니다.")
                    try:
                        self._new_context()
                    except Exception as recreate_err:
                        logger.error(f"컨텍스트 재생성 실패: {recreate_err}")

                if attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning(f"{delay:.1f}초 후 재시도합니다.")
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
        if not self._page_is_healthy():
            self._new_context()
        page = self._page
        self._request_count += 1

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

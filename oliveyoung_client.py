import logging
import random
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

BASE_URL = "https://www.oliveyoung.co.kr"
MAIN_URL = f"{BASE_URL}/store/main/main.do?oy=0"
RANKING_URL = (
    f"{BASE_URL}/store/main/getBestList.do"
    "?t_page=%ED%99%88&t_click=GNB&t_gnb_type=%EB%9E%AD%ED%82%B9&t_swiping_type=N"
)

MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 8.0
RETRY_BACKOFF_FACTOR = 2.5
RETRY_JITTER_SECONDS = 4.0
NAVIGATION_TIMEOUT_MS = 35_000
PRODUCT_WAIT_TIMEOUT_MS = 8_000
RECYCLE_AFTER_REQUESTS = 500

PRODUCT_LINK_SELECTOR = (
    'a[href*="getGoodsDetail.do"], '
    'a[href*="goodsNo="], '
    'a[href*="gdsNo="]'
)

# These are block/challenge signals only. Normal pages may also contain a
# generic browser compatibility notice, so that notice is intentionally absent.
BLOCK_PATTERNS = (
    "접속 정보를 확인 중",
    "비정상적인 접근",
    "자동화된 접근",
    "just a moment",
    "verify you are human",
    "cf-chl-",
    "access denied",
    "forbidden",
    "too many requests",
    "temporarily blocked",
    "request blocked",
)


def _retry_delay(attempt: int) -> float:
    delay = BASE_RETRY_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
    return delay + random.uniform(0, RETRY_JITTER_SECONDS)


def _norm_text(value: str) -> str:
    return " ".join((value or "").split())


def _looks_blocked(html: str) -> bool:
    text = _norm_text(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)).lower()
    return any(pattern.lower() in text for pattern in BLOCK_PATTERNS)


def _response_status(response) -> int | None:
    try:
        return response.status if response is not None else None
    except Exception:
        return None


class OliveYoungClient:
    """
    Olive Young public-page client.

    The previous version hard-coded an old Chrome UA and relied on a single,
    fragile product-list selector. This version lets Playwright use the UA of
    the installed Chromium, keeps one browser/context for the whole run,
    primes the session on the normal main page, detects HTTP/block pages,
    waits on product links instead of legacy list-class names, and captures a
    small diagnostic HTML file when a page is suspicious.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._request_count = 0
        self._primed = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        if self._context is None or self._page is None or self._page.is_closed():
            self._new_context()

        if not self._primed:
            self._prime_session()

    def _new_context(self):
        self._close_context_only()
        self._context = self._browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
        )
        # Do not spoof navigator.webdriver. Let the installed browser expose
        # its normal values; this is both less brittle and easier to diagnose.
        self._context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_()
            ),
        )
        self._page = self._context.new_page()
        self._request_count = 0
        self._primed = False

    def _prime_session(self):
        """Open the public main page once so normal site cookies/session state exist."""
        page = self._page
        try:
            response = page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            status = _response_status(response)
            html = page.content()
            logger.info("OliveYoung session prime: status=%s html=%s url=%s", status, len(html), page.url)
            if status in (403, 429):
                raise RuntimeError(f"Olive Young session prime HTTP {status}")
            if _looks_blocked(html):
                raise RuntimeError("Olive Young session prime returned a block/challenge page")
            self._primed = True
            self._request_count += 1
        except Exception:
            # Do not mark as primed. The actual requested URL will retry with
            # the same context, and the caller will get a useful error if it fails.
            logger.warning("OliveYoung session prime failed; continuing to requested URL", exc_info=True)

    def _close_context_only(self):
        for obj in (self._page, self._context):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._primed = False

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

    def _page_is_healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def _capture_diagnostic(self, html: str, label: str):
        try:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
            filename = f"oliveyoung_debug_{safe}_{int(time.time())}.html"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html or "")
            logger.warning("OliveYoung diagnostic saved: %s", filename)
        except Exception:
            logger.exception("Failed to save OliveYoung diagnostic HTML")

    def _wait_for_products(self, page):
        try:
            page.wait_for_selector(PRODUCT_LINK_SELECTOR, timeout=PRODUCT_WAIT_TIMEOUT_MS)
            return True
        except PlaywrightTimeoutError:
            return False

    def _fetch_once(self, url: str):
        self.start()
        if self._request_count >= RECYCLE_AFTER_REQUESTS:
            logger.info("Recycling OliveYoung browser context after %s requests", RECYCLE_AFTER_REQUESTS)
            self._new_context()
            self._prime_session()

        if not self._page_is_healthy():
            self._new_context()
            self._prime_session()

        page = self._page
        self._request_count += 1

        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except Exception:
            if not self._page_is_healthy():
                raise
            # A navigation timeout can still leave a fully useful DOM.
            logger.warning("OliveYoung navigation exception for %s", url, exc_info=True)
            response = None

        status = _response_status(response)
        html = page.content()
        title = page.title()
        product_wait = self._wait_for_products(page)
        html = page.content()

        logger.info(
            "OliveYoung fetch: status=%s title=%r html=%s products_wait=%s url=%s",
            status, title, len(html), product_wait, page.url,
        )

        if status in (401, 403, 429, 451, 503):
            self._capture_diagnostic(html, f"http_{status}")
            raise RuntimeError(f"Olive Young HTTP {status}: {url}")

        if _looks_blocked(html):
            self._capture_diagnostic(html, "blocked_or_challenge")
            raise RuntimeError(f"Olive Young returned a block/challenge page: {url}")

        # A real empty catalog page is allowed. The parser/collector decides
        # whether it is a normal end-of-pagination condition.
        return html

    def _fetch_with_browser(self, url: str):
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self._fetch_once(url)
            except Exception as exc:
                last_error = exc
                logger.warning("OliveYoung request failed (%s/%s): %s", attempt, MAX_RETRIES, exc)
                if attempt >= MAX_RETRIES:
                    break

                if not self._page_is_healthy() or "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                    logger.info("Creating a fresh OliveYoung browser context before retry")
                    self._new_context()

                delay = _retry_delay(attempt)
                logger.info("Retrying OliveYoung request in %.1fs", delay)
                time.sleep(delay)

        raise last_error or RuntimeError(f"OliveYoung request failed: {url}")

    def discover_subcategories(self, parent_disp_cat_no):
        url = (
            f"{BASE_URL}/store/display/getCategoryShop.do"
            f"?dispCatNo={parent_disp_cat_no}"
        )
        html = self._fetch_with_browser(url)
        soup = BeautifulSoup(html, "html.parser")
        subcategories = []
        seen = set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            m = re.search(r"dispCatNo=(\d+)", href)
            if not m:
                continue
            code = m.group(1)
            if code == parent_disp_cat_no or not code.startswith(parent_disp_cat_no):
                continue
            if code in seen:
                continue
            seen.add(code)
            name = _norm_text(link.get_text(" ", strip=True)) or f"sub_{code[-4:]}"
            subcategories.append({"name": name, "disp_cat_no": code})
        logger.info("[%s] 세부카테고리 발견: %s개", parent_disp_cat_no, len(subcategories))
        return subcategories

    def fetch_top100(self):
        return self._fetch_with_browser(RANKING_URL)

    def fetch_category_page(self, disp_cat_no, page_idx=1, rows_per_page=48):
        if len(str(disp_cat_no)) > 11:
            base = f"{BASE_URL}/store/display/getMCategoryList.do"
            tracking = f"Cat{disp_cat_no}_Small"
        else:
            base = f"{BASE_URL}/store/display/getCategoryShop.do"
            tracking = f"Cat{disp_cat_no}_MID"

        # Keep the public endpoint but include the normal visible-page query
        # parameters used by Olive Young's category navigation. The old client
        # sent only four parameters, which became fragile after site changes.
        params = (
            f"dispCatNo={disp_cat_no}"
            f"&fltDispCatNo="
            f"&prdSort=01"
            f"&pageIdx={page_idx}"
            f"&rowsPerPage={rows_per_page}"
            f"&searchTypeSort=btn_thumb"
            f"&plusButtonFlag=N"
            f"&isLoginCnt=0"
            f"&aShowCnt=0"
            f"&bShowCnt=0"
            f"&cShowCnt=0"
            f"&trackingCd={tracking}"
        )
        url = f"{base}?{params}"
        return self._fetch_with_browser(url)

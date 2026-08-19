import logging
import re
import time
import random
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from curl_cffi import requests

logger = logging.getLogger(__name__)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

class OliveYoungClient:
    def __init__(self):
        # 🚨 핵심: chrome120 브라우저인 척 위장 (TLS 핑거프린트 복제)
        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": "https://www.oliveyoung.co.kr/",
        }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def _fetch(self, url, max_retries=3):
        """실제 HTTP 요청을 보내는 함수 (재시도 로직 포함)"""
        for attempt in range(1, max_retries + 1):
            try:
                # 타임아웃 10초 설정
                response = self.session.get(url, headers=self.headers, timeout=10)
                
                if response.status_code == 403:
                    raise Exception(f"HTTP Error 403 (차단됨): {url}")
                
                return response.text
            
            except Exception as e:
                logger.warning(f"요청 실패({attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2 + random.uniform(0, 2))  # 백오프 재시도
                else:
                    raise

    def fetch_top100(self):
        """랭킹 100개 수집"""
        url = (
            "https://www.oliveyoung.co.kr/store/main/getBestList.do"
            "?t_page=%ED%99%88&t_click=GNB&t_gnb_type=%EB%9E%AD%ED%82%B9&t_swiping_type=N"
        )
        return self._fetch(url)

    def fetch_category_page(self, disp_cat_no, page_idx=1, rows_per_page=48):
        """카테고리 페이지 수집"""
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
        return self._fetch(url)

    def discover_subcategories(self, parent_disp_cat_no):
        """세부 카테고리 탐색 (기존 로직과 동일)"""
        url = f"https://www.oliveyoung.co.kr/store/display/getCategoryShop.do?dispCatNo={parent_disp_cat_no}"
        html = self._fetch(url)
        
        soup = BeautifulSoup(html, "html.parser")
        subcategories = []
        seen = set()

        for link in soup.find_all("a", href=True):
            m = re.search(r"dispCatNo=(\d+)", link.get("href", ""))
            if not m:
                continue

            code = m.group(1)
            if code == parent_disp_cat_no or not code.startswith(parent_disp_cat_no) or code in seen:
                continue

            seen.add(code)
            name = link.get_text(strip=True) or f"sub_{code[-4:]}"
            subcategories.append({"name": name, "disp_cat_no": code})

        logger.info(f"[{parent_disp_cat_no}] 세부카테고리 발견: {len(subcategories)}개")
        return subcategories

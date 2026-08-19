import random
import time
import logging

from curl_cffi import requests as curl_requests

import daiso_config as dconfig

logger = logging.getLogger(__name__)


def _retry_delay(attempt):
    """attempt(1부터 시작)에 따라 점점 늘어나는 대기시간(초)을 계산."""
    delay = dconfig.DAISO_BASE_RETRY_DELAY_SECONDS * (
        dconfig.DAISO_RETRY_BACKOFF_FACTOR ** (attempt - 1)
    )
    return delay + random.uniform(0, dconfig.DAISO_RETRY_JITTER_SECONDS)


class DaisoClient:
    """
    다이소몰은 브라우저 렌더링 없이 SearchGoods JSON API를 직접 호출하면 되므로
    Playwright 대신 curl_cffi(TLS 핑거프린트를 크롬처럼 위장)로 가볍게 처리한다.

    핵심: isCategory=1 파라미터가 없으면 exhCtgrNo/largeExhCtgrNo가 무시되고
    카테고리와 무관한 추천/트렌딩 결과가 반환됨(직접 검증 완료). 이 클라이언트는
    실제 카테고리 페이지에서 캡처한 파라미터 세트를 그대로 재현한다.
    """

    def __init__(self):
        self.session = curl_requests.Session()

    def _headers(self, middle_ctgr_no):
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "ko,en;q=0.9,en-US;q=0.8",
            "referer": dconfig.daiso_referer(middle_ctgr_no),
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": dconfig.DAISO_USER_AGENT,
        }

    def _fetch_once(self, middle_ctgr_no, page_num, rows_per_page):
        params = dict(dconfig.DAISO_SEARCH_STATIC_PARAMS)
        params.update({
            "pageNum": str(page_num),
            "cntPerPage": str(rows_per_page),
            "exhCtgrNo": middle_ctgr_no,
            "largeExhCtgrNo": dconfig.DAISO_LARGE_CTGR_NO,
        })

        resp = self.session.get(
            dconfig.DAISO_SEARCH_URL,
            params=params,
            headers=self._headers(middle_ctgr_no),
            impersonate="chrome124",
            timeout=dconfig.DAISO_REQUEST_TIMEOUT,
        )

        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.url}")

        data = resp.json()

        if data.get("returnCode") != 1 or data.get("status") != 200:
            raise Exception(f"API 오류 응답(returnCode/status 이상): {data.get('returnCode')}/{data.get('status')}")

        return data

    def fetch_category_page(self, middle_ctgr_no, page_num=1, rows_per_page=None):
        """
        뷰티/위생(CTGR_01050) 하위 중분류(middle_ctgr_no) 상품 목록을
        페이지네이션하며 수집. 재시도 시 지수 백오프 + 지터 사용.
        """
        if rows_per_page is None:
            rows_per_page = dconfig.DAISO_ROWS_PER_PAGE

        last_error = None
        for attempt in range(1, dconfig.DAISO_MAX_RETRIES + 1):
            try:
                return self._fetch_once(middle_ctgr_no, page_num, rows_per_page)
            except Exception as e:
                last_error = e
                if attempt < dconfig.DAISO_MAX_RETRIES:
                    delay = _retry_delay(attempt)
                    logger.warning(
                        f"[다이소] 요청 실패({attempt}/{dconfig.DAISO_MAX_RETRIES}), "
                        f"{delay:.1f}초 후 재시도: {e} "
                        f"(exhCtgrNo={middle_ctgr_no}, page={page_num})"
                    )
                    time.sleep(delay)

        raise last_error or Exception(
            f"요청 실패: exhCtgrNo={middle_ctgr_no}, page={page_num}"
        )

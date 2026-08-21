import re
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup

BASE_URL = "https://www.oliveyoung.co.kr"

# 🚨 기획전 감지 키워드 (DB에는 is_bundle로 저장되나, 랭킹 집계에서는 이제 제외하지 않음)
BUNDLE_KEYWORDS = [
    "기획", "1+1", "2+1", "3+1", "세트", "증정", "리필",
    "선물", "한정", "키트", "팩+토너", "미니", "샘플",
    "기획전", "단독기획", "더블기획", "트리플기획",
]


def is_bundle_product(name):
    """상품명에 기획전 키워드가 포함되어 있는지 확인"""
    if not name:
        return False
    return any(kw in name for kw in BUNDLE_KEYWORDS)


def clean_text(el):
    if not el:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())


def product_id_from_url(url):
    if not url:
        return None
    q = parse_qs(urlparse(url).query)
    for key in ("goodsNo", "goodsno", "gdsNo", "gdsno"):
        values = q.get(key)
        if values and values[0].strip():
            return f"OY_{values[0].strip()}"
    m = re.search(r"(?:goodsNo|gdsNo)[=/\-]?([A-Za-z0-9]+)", url, re.I)
    return f"OY_{m.group(1)}" if m else None


def extract_product_link(item):
    for a in item.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("javascript:"):
            continue
        absolute = urljoin(BASE_URL, href)
        if (
            "getGoodsDetail.do" in absolute
            or "goodsNo=" in absolute
            or "gdsNo=" in absolute
        ):
            return absolute
    for a in item.select("[data-ref-gdsNo], [data-ref-goodsNo]"):
        goods_no = a.get("data-ref-gdsNo") or a.get("data-ref-goodsNo")
        if goods_no:
            return f"{BASE_URL}/store/goods/getGoodsDetail.do?goodsNo={goods_no}"
    return ""


def extract_brand(item):
    for sel in (".tx_brand", ".brand", "[class*='brand']"):
        el = item.select_one(sel)
        if el and clean_text(el):
            return clean_text(el)
    return ""


def extract_name(item):
    for sel in (
        ".tx_name", ".prd_name", ".goods_name",
        "[class*='tx_name']", "[class*='goods_name']",
    ):
        el = item.select_one(sel)
        if el and clean_text(el):
            return clean_text(el)
    return ""


def _nums_from_text(text):
    """텍스트에서 유효한 가격 숫자만 추출 (999/1000원 미만 노이즈 제거)"""
    if not text:
        return []
    cleaned = re.sub(r"\d+\s*[%+~]", " ", text)          # 할인율 제거
    cleaned = re.sub(r"[\(\[].*?[\)\]]", " ", cleaned)  # 리뷰/괄호 제거
    vals = []
    for n in re.findall(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{3,7})(?!\d)", cleaned):
        try:
            v = int(n.replace(",", ""))
        except ValueError:
            continue
        if v == 999 or v < 1000:
            continue
        vals.append(v)
    return vals


def extract_prices(item):
    price_text = " ".join(
        clean_text(x) for x in item.select(
            ".price, .prd_price, .tx_price, .tx_num, .num, "
            "[class*='price'], [class*='Price'], [class*='cost'], [class*='amount']"
        )
    )
    values = _nums_from_text(price_text)

    if not values:
        for attr in ("data-price", "data-sell-price", "data-org-price",
                     "data-goods-price", "data-prc"):
            el = item.select_one(f"[{attr}]")
            if el:
                values = _nums_from_text(el.get(attr, ""))
                if values:
                    break

    if not values:
        text = clean_text(item)
        won_vals = []
        for n in re.findall(r"(\d{1,3}(?:,\d{3})+|\d{3,7})\s*원", text):
            try:
                v = int(n.replace(",", ""))
            except ValueError:
                continue
            if v == 999 or v < 1000:
                continue
            won_vals.append(v)
        values = won_vals

    if not values:
        return None, None

    price, sale_price = (values[0], values[-1]) if len(values) >= 2 else (values[0], None)
    if price and sale_price and sale_price > price:
        price, sale_price = sale_price, price
    return price, sale_price


def candidate_product_items(soup):
    selectors = [
        ".cate_prd_list > li",
        ".cate_prd_list li",
        ".prd_list li",
        "li",
    ]
    for sel in selectors:
        found = soup.select(sel)
        product_like = [
            x for x in found
            if extract_product_link(x) and extract_name(x)
        ]
        if len(product_like) >= 10:
            return product_like
    return []


def parse_products(html, category=""):
    soup = BeautifulSoup(html, "html.parser")
    items = candidate_product_items(soup)
    out = []
    seen = set()
    for item in items:
        url = extract_product_link(item)
        pid = product_id_from_url(url)
        if not pid or pid in seen:
            continue
        name = extract_name(item)
        if not name:
            continue
        price, sale_price = extract_prices(item)
        out.append({
            "product_id": pid,
            "source": "oliveyoung",
            "brand": extract_brand(item),
            "product_name": name,
            "product_url": url,
            "category": category,
            "price": price,
            "sale_price": sale_price,
            "is_bundle": is_bundle_product(name),
        })
        seen.add(pid)
    return out


def parse_ranked_products(html, category="ALL", limit=100):
    """
    🚨 기획전, 1+1 여부 상관없이 사이트에 노출된 순서대로 1~100위 랭킹을 매깁니다.
    (is_bundle 여부는 DB에 그대로 저장되므로, 나중에 분석할 때만 필터링하면 됩니다.)
    """
    products = parse_products(html, category)
    
    # 페이지에 노출된 순서(1~100) 그대로 랭킹 부여
    for rank_counter, p in enumerate(products, start=1):
        if rank_counter <= limit:
            p["rank"] = rank_counter
        else:
            p["rank"] = None
            
    # 랭킹이 부여된 상품(1~limit)만 반환
    return [p for p in products if p.get("rank") is not None][:limit]

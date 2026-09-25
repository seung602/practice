import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

BASE_URL = "https://www.oliveyoung.co.kr"

BUNDLE_KEYWORDS = [
    "기획", "1+1", "2+1", "3+1", "세트", "증정", "리필",
    "선물", "한정", "키트", "팩+토너", "미니", "샘플",
    "기획전", "단독기획", "더블기획", "트리플기획",
]

PRODUCT_LINK_SELECTOR = (
    'a[href*="getGoodsDetail.do"], '
    'a[href*="goodsNo="], '
    'a[href*="gdsNo="]'
)

NAME_SELECTORS = (
    ".tx_name",
    ".prd_name",
    ".goods_name",
    ".prd-name",
    ".goods-name",
    "[class*='tx_name']",
    "[class*='goods_name']",
    "[class*='prd_name']",
    "[class*='product_name']",
    "[class*='product-name']",
    "p[class*='name']",
    "strong[class*='name']",
)

BRAND_SELECTORS = (
    ".tx_brand",
    ".brand",
    ".prd_brand",
    "[class*='brand']",
)

PRICE_SELECTORS = (
    ".price",
    ".prd_price",
    ".tx_price",
    ".tx_num",
    ".num",
    "[class*='price']",
    "[class*='Price']",
    "[class*='cost']",
    "[class*='amount']",
)

CARD_CLASS_HINTS = (
    "prd_info",
    "prd-unit",
    "prd_list",
    "cate_prd_list",
    "prd_item",
    "product",
    "goods",
)


def clean_text(value):
    return " ".join((value or "").split())


def is_bundle_product(name):
    return bool(name) and any(kw in name for kw in BUNDLE_KEYWORDS)


def _extract_goods_no(url):
    if not url:
        return None
    raw = unquote(url)
    parsed = urlparse(raw)
    q = parse_qs(parsed.query)
    for key in ("goodsNo", "goodsno", "gdsNo", "gdsno"):
        values = q.get(key)
        if values and values[0].strip():
            return values[0].strip()

    for pattern in (
        r"(?:goodsNo|gdsNo)\s*[=:/\-]\s*['\"]?([A-Za-z0-9_-]+)",
        r"(?:goodsNo|gdsNo)\s*\(\s*['\"]?([A-Za-z0-9_-]+)",
    ):
        m = re.search(pattern, raw, re.I)
        if m:
            return m.group(1)
    return None


def product_id_from_url(url):
    goods_no = _extract_goods_no(url)
    return f"OY_{goods_no}" if goods_no else None


def extract_product_link(item):
    if item is None:
        return ""

    if getattr(item, "name", None) == "a":
        candidates = [item]
    else:
        candidates = item.select("a[href], [data-ref-gdsNo], [data-ref-goodsNo]")

    for node in candidates:
        href = (node.get("href") or "").strip()
        if href and not href.lower().startswith("javascript:"):
            absolute = urljoin(BASE_URL, href)
            if _extract_goods_no(absolute):
                return absolute

        for key in ("data-ref-gdsNo", "data-ref-goodsNo"):
            goods_no = (node.get(key) or "").strip()
            if goods_no:
                return f"{BASE_URL}/store/goods/getGoodsDetail.do?goodsNo={goods_no}"

        if href.lower().startswith("javascript:"):
            goods_no = _extract_goods_no(href)
            if goods_no:
                return f"{BASE_URL}/store/goods/getGoodsDetail.do?goodsNo={goods_no}"

    return ""


def _first_text(item, selectors):
    for sel in selectors:
        try:
            el = item.select_one(sel)
        except Exception:
            el = None
        if el:
            text = clean_text(el.get_text(" ", strip=True))
            if text:
                return text
    return ""




def _strip_leading_rank(value):
    value = clean_text(value)
    # Ranking cards may render the rank number inside the same <a> as the
    # product name (for example: "76 셀리맥스 ..."). Remove only a leading
    # 1~3 digit rank followed by whitespace/separator; numeric-only names are
    # rejected separately below.
    value = re.sub(r"^\d{1,3}\s+(?=\S)", "", value)
    return value

def extract_brand(item):
    return _first_text(item, BRAND_SELECTORS)


def extract_name(item):
    value = _first_text(item, NAME_SELECTORS)
    if value:
        return value

    for attr in ("aria-label", "title", "alt"):
        value = clean_text(item.get(attr, "")) if hasattr(item, "get") else ""
        if value:
            return value

    # The ranking page can contain two links for the same product: a short
    # rank-number link and a second link containing the real product name.
    # Never accept a lone numeric rank such as "01" as the product name.
    try:
        links = [item] if getattr(item, "name", None) == "a" else item.select(PRODUCT_LINK_SELECTOR)
    except Exception:
        links = []
    candidates = []
    for link in links:
        value = clean_text(link.get("aria-label", "")) or clean_text(link.get("title", ""))
        if not value:
            value = clean_text(link.get_text(" ", strip=True))
        value = _strip_leading_rank(value)
        if value and not re.fullmatch(r"\d{1,3}", value):
            candidates.append(value)
    if candidates:
        return max(candidates, key=len)

    try:
        img = item.select_one("img[alt]")
    except Exception:
        img = None
    if img:
        value = _strip_leading_rank(clean_text(img.get("alt", "")))
        if value and not re.fullmatch(r"\d{1,3}", value):
            return value

    return ""


def _nums_from_text(text):
    if not text:
        return []
    cleaned = re.sub(r"\d+\s*[%+~]", " ", text)
    cleaned = re.sub(r"[\(\[].*?[\)\]]", " ", cleaned)
    values = []
    for n in re.findall(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{3,7})(?!\d)", cleaned):
        try:
            value = int(n.replace(",", ""))
        except ValueError:
            continue
        if value == 999 or value < 1000:
            continue
        values.append(value)
    return values


def extract_prices(item):
    price_text = " ".join(
        clean_text(x.get_text(" ", strip=True))
        for x in item.select(", ".join(PRICE_SELECTORS))
    )
    values = _nums_from_text(price_text)

    if not values:
        for attr in (
            "data-price", "data-sell-price", "data-org-price",
            "data-goods-price", "data-prc", "data-price-value",
        ):
            el = item.select_one(f"[{attr}]")
            if el:
                values = _nums_from_text(el.get(attr, ""))
                if values:
                    break

    if not values:
        values = _nums_from_text(clean_text(item.get_text(" ", strip=True)))

    if not values:
        return None, None

    original_price = values[0]
    sale_price = values[-1] if len(values) >= 2 else None
    if sale_price is not None and sale_price > original_price:
        original_price, sale_price = sale_price, original_price
    return original_price, sale_price


def _product_links(soup):
    """Return product detail links in document order, de-duplicated by goodsNo."""
    out = []
    seen = set()
    for link in soup.select(PRODUCT_LINK_SELECTOR):
        href = extract_product_link(link)
        pid = product_id_from_url(href)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append((link, pid, href))
    return out


def _container_for_link(link, pid):
    # Prefer the nearest LI when it represents one product card.
    parent = link.parent
    for _ in range(8):
        if parent is None:
            break
        if getattr(parent, "name", None) in ("li", "article"):
            links = [product_id_from_url(extract_product_link(x)) for x in parent.select(PRODUCT_LINK_SELECTOR)]
            links = [x for x in links if x]
            if len(set(links)) == 1 and pid in links:
                return parent
        parent = parent.parent

    # Otherwise choose the nearest ancestor with a product-ish class.
    parent = link.parent
    for _ in range(8):
        if parent is None:
            break
        classes = " ".join(parent.get("class", [])) if getattr(parent, "get", None) else ""
        if any(hint in classes for hint in CARD_CLASS_HINTS):
            return parent
        parent = parent.parent

    return link


def _parse_item(link, pid, url, category):
    container = _container_for_link(link, pid)
    name = extract_name(container)
    if not name:
        name = extract_name(link)
    if not name:
        return None

    price, sale_price = extract_prices(container)
    return {
        "product_id": pid,
        "source": "oliveyoung",
        "brand": extract_brand(container),
        "product_name": name,
        "product_url": url,
        "category": category,
        "price": price,
        "sale_price": sale_price,
        "is_bundle": is_bundle_product(name),
    }


def parse_products(html, category=""):
    soup = BeautifulSoup(html or "", "html.parser")
    pairs = _product_links(soup)
    out = []
    seen = set()

    for link, pid, url in pairs:
        if pid in seen:
            continue
        item = _parse_item(link, pid, url, category)
        if item is None:
            continue
        out.append(item)
        seen.add(pid)

    return out


def parse_ranked_products(html, category="ALL", limit=100):
    products = parse_products(html, category)
    for rank_counter, product in enumerate(products, start=1):
        product["rank"] = rank_counter if rank_counter <= limit else None
    return [p for p in products if p.get("rank") is not None][:limit]

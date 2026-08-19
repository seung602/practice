import daiso_config as dconfig


def _find_documents_block(api_response):
    """
    resultSet.result 는 보통 [파셋/그룹결과, 실제상품목록] 구조지만,
    구조가 바뀔 가능성에 대비해 'resultDocuments' 키를 가진 블록을 찾는다.
    못 찾으면 None, None 반환(수집 실패로 처리하도록).
    """
    try:
        result_list = api_response["resultSet"]["result"]
    except (KeyError, TypeError):
        return None, None

    for block in result_list:
        if isinstance(block, dict) and "resultDocuments" in block:
            return block.get("resultDocuments") or [], block.get("totalSize")

    return None, None


def _to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None


def _to_float(value):
    """평점(avgStscVal) 변환용 실수 파싱 함수"""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def parse_products(api_response, category=""):
    """
    SearchGoods API 응답(JSON dict)을 상품 리스트로 변환.
    올리브영 파서와 동일한 스키마를 맞춰 DB 저장 함수(db.upsert_product 등)를
    소스 구분 없이 그대로 재사용할 수 있게 한다.
    """
    documents, total_size = _find_documents_block(api_response)
    if documents is None:
        raise Exception("SearchGoods 응답에서 resultDocuments를 찾을 수 없음 (구조 변경 의심)")

    out = []
    seen = set()

    for doc in documents:
        pd_no = doc.get("pdNo")
        if not pd_no:
            continue

        product_id = f"DS_{pd_no}"
        if product_id in seen:
            continue

        name = doc.get("exhPdNm") or doc.get("pdNm") or ""
        if not name:
            continue

        out.append({
            "product_id": product_id,
            "source": "daiso",
            "brand": doc.get("brndNm") or "",
            "product_name": name,
            "product_url": dconfig.DAISO_PRODUCT_URL_TEMPLATE.format(pd_no=pd_no),
            "category": doc.get("exhMiddleCtgrNm") or category,
            "price": _to_int(doc.get("pdPrc")),
            "sale_price": None,
            "sold_out": doc.get("soldOutYn") == "Y",
            "small_category": doc.get("exhSmallCtgrNm") or "",
            "review_count": _to_int(doc.get("revwCnt")),
            "rating": _to_float(doc.get("avgStscVal")),
            "is_new": doc.get("newPdYn") == "Y",
        })
        seen.add(product_id)

    return out, total_size

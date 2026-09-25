from oliveyoung_parser import parse_products, parse_ranked_products


def make_html(n=100):
    rows = []
    for i in range(1, n + 1):
        rows.append(f"""
        <li>
          <a class="product-link" href="/store/goods/getGoodsDetail.do?goodsNo=A{i:012d}">{i:02d}</a>
          <div class="info">
            <a class="product-link" href="/store/goods/getGoodsDetail.do?goodsNo=A{i:012d}">브랜드{i} 상품 {i} 50ml</a>
            <span class="brand">브랜드{i}</span>
            <span class="price">20,000 원</span><span class="price">10,000 원</span>
          </div>
        </li>""")
    return "<html><body><ul>" + "".join(rows) + "</ul></body></html>"


html = make_html()
items = parse_products(html, category="ALL")
assert len(items) == 100, len(items)
assert items[0]["product_id"] == "OY_A000000000001"
assert items[0]["product_name"] == "브랜드1 상품 1 50ml", items[0]
assert items[0]["brand"] == "브랜드1"
assert items[0]["price"] == 20000 and items[0]["sale_price"] == 10000

ranked = parse_ranked_products(html, category="ALL", limit=100)
assert len(ranked) == 100
assert [x["rank"] for x in ranked] == list(range(1, 101))
assert ranked[99]["product_id"] == "OY_A000000000100"
print("PARSER REGRESSION 100 OK")

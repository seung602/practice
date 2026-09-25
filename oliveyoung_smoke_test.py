import logging

import config
from oliveyoung_client import OliveYoungClient
from oliveyoung_parser import parse_products, parse_ranked_products

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


RANKING_MIN = 80
CATEGORY_MIN = 20


def main():
    with OliveYoungClient() as client:
        ranking_html = client.fetch_top100()
        ranking = parse_ranked_products(ranking_html, category="ALL", limit=100)

        parent_name, parent_code = next(
            (name, code) for name, code in config.RANKING_CATEGORIES if code
        )
        sub_name, sub_code = next(
            (name, code) for name, code in config.SUBCATEGORIES.get(parent_name, [])
        )
        category_html = client.fetch_category_page(
            sub_code, page_idx=1, rows_per_page=config.ROWS_PER_PAGE
        )
        category = parse_products(category_html, category=sub_name)

    print(f"SMOKE RANKING={len(ranking)}")
    print(f"SMOKE CATEGORY={len(category)}")

    ranking_ids = [p.get("product_id") for p in ranking]
    if len(ranking) < RANKING_MIN:
        raise SystemExit(f"ranking parse failed: {len(ranking)} < {RANKING_MIN}")
    if len(set(ranking_ids)) != len(ranking_ids):
        raise SystemExit("ranking parse failed: duplicate product_id")
    if [p.get("rank") for p in ranking] != list(range(1, len(ranking) + 1)):
        raise SystemExit("ranking parse failed: rank sequence is not contiguous")

    category_ids = [p.get("product_id") for p in category]
    if len(category) < CATEGORY_MIN:
        raise SystemExit(f"category parse failed: {len(category)} < {CATEGORY_MIN}")
    if len(set(category_ids)) != len(category_ids):
        raise SystemExit("category parse failed: duplicate product_id")

    print("SMOKE OK")
    print("RANKING_SAMPLE:", ranking[0])
    print("CATEGORY_SAMPLE:", category[0])


if __name__ == "__main__":
    main()

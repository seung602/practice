import time
import logging
from datetime import datetime
import db
import daiso_config as dconfig
from daiso_client import DaisoClient
from daiso_parser import parse_products

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def collect_daiso_catalog():
    """
    dconfig.DAISO_CATEGORIES(뷰티/위생 하위 중분류, 화장품만)를
    페이지가 끝날 때까지(반환 개수 < ROWS_PER_PAGE) 순회하며 전체 상품을 수집.
    product_id 기준으로 중복 제거. 올리브영 catalog_collector.py와 동일한 패턴.
    """
    client = DaisoClient()
    all_products = {}
    surfaces_fetched = 0
    pages_fetched = 0
    failed_categories = []

    for category_name, middle_ctgr_no in dconfig.DAISO_CATEGORIES:
        surfaces_fetched += 1
        page_num = 1
        category_count = 0
        category_failed = False

        while page_num <= dconfig.DAISO_MAX_PAGES_PER_CATEGORY:
            try:
                api_response = client.fetch_category_page(middle_ctgr_no, page_num)
            except Exception as e:
                logging.error(f"[{category_name}] page {page_num} 요청 실패: {e}")
                category_failed = True
                break

            try:
                products, total_size = parse_products(api_response, category=category_name)
            except Exception as e:
                logging.error(f"[{category_name}] page {page_num} 파싱 실패: {e}")
                category_failed = True
                break

            pages_fetched += 1

            if not products:
                logging.info(f"[{category_name}] page {page_num}: 0개 (종료)")
                break

            new_count = sum(1 for p in products if p["product_id"] not in all_products)
            for p in products:
                all_products[p["product_id"]] = p
            category_count += len(products)

            logging.info(
                f"[{category_name}] page {page_num}: {len(products)}개 수집 "
                f"(신규 {new_count}) / 카테고리 누적 {category_count}"
                + (f"/{total_size}" if total_size is not None else "")
            )

            if len(products) < dconfig.DAISO_ROWS_PER_PAGE:
                break

            page_num += 1
            time.sleep(dconfig.DAISO_REQUEST_DELAY_SECONDS)

        if category_failed:
            failed_categories.append(category_name)

    return list(all_products.values()), surfaces_fetched, pages_fetched, failed_categories


def run_daiso_catalog_collection():
    now = datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    started_at = now.strftime("%Y-%m-%d %H:%M:%S")

    logging.info("=== 다이소몰 뷰티 카탈로그 수집 시작 ===")
    products, surfaces, pages, failed_categories = collect_daiso_catalog()
    unique_count = len(products)

    logging.info(
        f"다이소 카탈로그 수집 완료: 카테고리 {surfaces}개 / 페이지 {pages}개 / 고유 상품 {unique_count}개"
    )

    conn = db.connect()
    status = "SUCCESS"
    stats = {"NEW": 0, "CHANGED": 0, "UNCHANGED": 0}

    try:
        if unique_count < dconfig.MIN_DAISO_CATALOG_ITEMS:
            status = "FAILED_MIN_ITEMS"
            logging.error(
                f"수집량({unique_count})이 MIN_DAISO_CATALOG_ITEMS({dconfig.MIN_DAISO_CATALOG_ITEMS}) "
                f"미만이라 상품 상태(ACTIVE/MISSING) 갱신은 건너뜁니다."
            )
        else:
            seen_ids = set()
            for p in products:
                p["daiso_score"] = db.compute_daiso_score(p)
                db.upsert_product(conn, p, started_at)
                change_type = db.save_snapshot(conn, p, run_date, started_at)
                stats[change_type] += 1
                seen_ids.add(p["product_id"])

            logging.info(
                f"다이소 스냅샷 저장 통계: 신규 {stats['NEW']}개 / "
                f"변경 {stats['CHANGED']}개 / 미변경 {stats['UNCHANGED']}개"
            )
            # ✅ 텔레그램 알림용 고정 포맷 마커
            logging.info(f"METRIC DAISO_CATALOG_COLLECTED={unique_count}")
            logging.info(f"METRIC DAISO_CATALOG_NEW={stats['NEW']}")

            db.update_daiso_rankings(conn, run_date)

            if failed_categories:
                status = "PARTIAL_CATEGORY_FAILURE"
                logging.error(
                    f"다음 카테고리는 재시도 후에도 수집 실패: {failed_categories} - "
                    f"MISSING 상태 갱신을 건너뜁니다. (수집된 상품 upsert는 정상 반영)"
                )
            else:
                db.mark_catalog_missing(conn, seen_ids, run_date, source="daiso")
                db.update_missing_status_transitions(conn, run_date, source="daiso")

        conn.execute(
            """
            INSERT INTO catalog_runs (
                source, run_date, started_at, finished_at, status,
                surfaces, pages, items_found, unique_products
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "daiso", run_date, started_at,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status, surfaces, pages, len(products), unique_count,
            ),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"다이소 카탈로그 저장 중 오류: {e}")
        raise
    finally:
        conn.close()

    return products, status, stats


def main():
    run_daiso_catalog_collection()


if __name__ == "__main__":
    main()

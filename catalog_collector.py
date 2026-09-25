import logging
import random
import time
from datetime import datetime

import config
import db
from oliveyoung_client import OliveYoungClient
from oliveyoung_parser import parse_products


def collect_catalog():
    all_products = {}
    total_pages_attempted = 0
    successful_pages = 0
    failed_pages_detail = []

    previously_failed = config.load_failed_pages()
    current_run_failed = {}
    parents = [c for c in config.RANKING_CATEGORIES if c[1]]

    with OliveYoungClient() as client:
        for parent_name, parent_code in parents:
            logging.info("\n=== [%s] 수집 시작 ===", parent_name)
            subs = getattr(config, "SUBCATEGORIES", {}).get(parent_name, [])
            if not subs:
                logging.warning("[%s] 세부카테고리 없음 - 대카테고리 직접 시도", parent_name)
                subs = [("전체", parent_code)]

            for sub_name, sub_code in subs:
                logging.info("[%s > %s] 수집 시작", parent_name, sub_name)
                page_idx = 1
                category_products = []
                consecutive_empty = 0
                sub_category_failed_pages = []
                max_pages = config.MAX_PAGES_PER_SURFACE

                while page_idx <= max_pages:
                    total_pages_attempted += 1
                    is_retry = page_idx in previously_failed.get(f"{parent_name}>{sub_name}", [])
                    if is_retry:
                        logging.warning("[%s > %s] page %s 이전 실패 기록 재시도", parent_name, sub_name, page_idx)

                    try:
                        raw_html = client.fetch_category_page(
                            sub_code, page_idx=page_idx, rows_per_page=config.ROWS_PER_PAGE
                        )
                        products = parse_products(raw_html, category=sub_name) or []
                        successful_pages += 1
                    except Exception as exc:
                        error = str(exc)
                        logging.error(
                            "[%s > %s] page %s 실패: %s",
                            parent_name, sub_name, page_idx, error,
                        )
                        sub_category_failed_pages.append(page_idx)
                        failed_pages_detail.append((f"{parent_name} > {sub_name}", page_idx, error))
                        page_idx += 1
                        continue

                    if not products:
                        consecutive_empty += 1
                        logging.info(
                            "[%s > %s] page %s: 0개 (빈 페이지 %s회)",
                            parent_name, sub_name, page_idx, consecutive_empty,
                        )
                        if consecutive_empty >= 3:
                            logging.info(
                                "[%s > %s] 빈 페이지 3회 연속 감지 - 종료",
                                parent_name, sub_name,
                            )
                            break
                    else:
                        consecutive_empty = 0
                        category_products.extend(products)
                        logging.info(
                            "[%s > %s] page %s: %s개 수집 (누적 %s개)",
                            parent_name, sub_name, page_idx, len(products), len(category_products),
                        )

                    page_idx += 1
                    time.sleep(random.uniform(config.REQUEST_DELAY_SECONDS, config.REQUEST_DELAY_SECONDS + 1.0))

                if sub_category_failed_pages:
                    current_run_failed[f"{parent_name}>{sub_name}"] = sub_category_failed_pages

                for product in category_products:
                    product["parent_category"] = parent_name
                    all_products[product["product_id"]] = product

    # Replace the prior failure list with the most recent run's failure list
    # for each affected surface. Do not let stale failures live forever.
    merged_failed = config.load_failed_pages()
    for key in list(current_run_failed.keys()):
        merged_failed[key] = current_run_failed[key]
    for parent_name, parent_code in parents:
        for sub_name, _ in getattr(config, "SUBCATEGORIES", {}).get(parent_name, []):
            key = f"{parent_name}>{sub_name}"
            if key not in current_run_failed:
                merged_failed.pop(key, None)
    config.save_failed_pages(merged_failed)

    return all_products, total_pages_attempted, successful_pages, failed_pages_detail


def run_catalog_collection():
    now = datetime.now()
    run_date = now.strftime("%Y-%m-%d")
    started_at = now.strftime("%Y-%m-%d %H:%M:%S")
    logging.info("=== 전체 상품 카탈로그 수집 시작 ===")

    products_dict, total_attempted, successful, failed_details = collect_catalog()
    products = list(products_dict.values())
    unique_count = len(products)
    failed_count = len(failed_details)

    logging.info("=" * 50)
    logging.info("올리브영 카탈로그 통계: 시도=%s 성공=%s 실패=%s 고유상품=%s",
                 total_attempted, successful, failed_count, unique_count)

    conn = db.connect()
    status = "SUCCESS"
    stats = {"NEW": 0, "CHANGED": 0, "UNCHANGED": 0}
    try:
        if unique_count < config.MIN_CATALOG_ITEMS:
            status = "FAILED_MIN_ITEMS"
            logging.error(
                "수집량(%s)이 MIN_CATALOG_ITEMS(%s) 미만. 기존 ACTIVE/MISSING 상태를 변경하지 않습니다.",
                unique_count, config.MIN_CATALOG_ITEMS,
            )
        else:
            seen_ids = set()
            for product in products:
                db.upsert_product(conn, product, started_at)
                change_type = db.save_snapshot(conn, product, run_date, started_at)
                stats[change_type] += 1
                seen_ids.add(product["product_id"])

            logging.info(
                "스냅샷 저장: 신규 %s / 변경 %s / 미변경 %s",
                stats["NEW"], stats["CHANGED"], stats["UNCHANGED"],
            )
            logging.info("METRIC OY_CATALOG_COLLECTED=%s", unique_count)
            logging.info("METRIC OY_CATALOG_NEW=%s", stats["NEW"])

            if failed_count > 0:
                status = "PARTIAL_PAGE_FAILURE"
                logging.warning("일부 페이지 실패로 기존 상품을 MISSING 처리하지 않습니다.")
            else:
                db.mark_catalog_missing(conn, seen_ids, run_date)
                db.update_missing_status_transitions(conn, run_date, source="oliveyoung")

        conn.execute(
            """
            INSERT INTO catalog_runs (
                source, run_date, started_at, finished_at, status,
                surfaces, pages, items_found, unique_products
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "oliveyoung",
                run_date,
                started_at,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status,
                len(config.RANKING_CATEGORIES) - 1,
                successful,
                len(products),
                unique_count,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return products, status, stats

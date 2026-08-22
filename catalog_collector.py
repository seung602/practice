import logging
import time
import random
import json
from datetime import datetime
import config
import db
from oliveyoung_client import OliveYoungClient
from oliveyoung_parser import parse_products


def collect_catalog():
    all_products = {}

    # 통계 추적 변수
    total_pages_attempted = 0
    successful_pages = 0
    failed_pages_detail = []  # [(카테고리명, 페이지번호, 에러메시지), ...]

    # 이전 실행에서 실패한 페이지 로드 (우선 재시도용)
    previously_failed = config.load_failed_pages()
    current_run_failed = {}

    parents = [c for c in config.RANKING_CATEGORIES if c[1]]  # ALL 제외

    # ⚠️ 이전 버전은 fetch 1번마다 브라우저를 새로 띄워서 매우 느렸음.
    # 브라우저는 전체 수집(모든 카테고리/페이지) 동안 1번만 띄우고 재사용한다.
    with OliveYoungClient() as client:
        for parent_name, parent_code in parents:
            logging.info(f"\n=== [{parent_name}] 수집 시작 ===")

            subs = []
            if hasattr(config, 'SUBCATEGORIES'):
                subs = config.SUBCATEGORIES.get(parent_name, [])

            if not subs:
                logging.warning(f"[{parent_name}] 세부카테고리 없음 - 대카테고리 직접 시도")
                subs = [("전체", parent_code)]

            for sub_name, sub_code in subs:
                logging.info(f"[{parent_name} > {sub_name}] 수집 시작")
                page_idx = 1
                category_products = []
                consecutive_empty = 0
                sub_category_failed_pages = []

                # 이전 실패 기록이 있으면 해당 페이지부터 시작하거나 우선 처리할 수 있도록 로직 구성
                # 여기서는 단순화를 위해 순차 진행하되, 실패 기록이 있으면 로그에 명시

                while page_idx <= config.MAX_PAGES_PER_SURFACE:
                    total_pages_attempted += 1
                    is_retry = page_idx in previously_failed.get(f"{parent_name}>{sub_name}", [])

                    if is_retry:
                        logging.warning(f"[{parent_name} > {sub_name}] page {page_idx}는 이전 실패 기록이 있어 재시도합니다.")

                    page_success = False
                    last_error = None

                    # ⚠️ 이전에는 여기서 3회, client 내부에서 또 3회 재시도해서
                    # 최악의 경우 9번(최대 수 분)까지 낭비되는 구조였음.
                    # 재시도는 client(oliveyoung_client.py) 쪽에서만 책임지고,
                    # 여기서는 결과만 받아서 처리한다(원칙 3 갱신: 재시도 책임 일원화).
                    try:
                        raw_html = client.fetch_category_page(
                            sub_code,
                            page_idx=page_idx,
                            rows_per_page=config.ROWS_PER_PAGE
                        )
                        products = parse_products(raw_html, category=sub_name)

                        # 원칙 6: 정상 상품 페이지인지 최소한 검증 (파서 내부에서 처리되지만, 여기선 None 체크)
                        if products is None:
                            products = []

                        page_success = True

                    except Exception as e:
                        last_error = str(e)
                        logging.error(
                            f"[{parent_name} > {sub_name}] page {page_idx} 실패. "
                            f"해당 페이지 건너뜀. (오류: {last_error})"
                        )

                    if not page_success:
                        # 원칙 1, 2: 실패해도 break 하지 않고 기록만 한 뒤 다음 페이지로 계속 진행
                        sub_category_failed_pages.append(page_idx)
                        failed_pages_detail.append((f"{parent_name} > {sub_name}", page_idx, last_error))
                        page_idx += 1
                        continue

                    # --- 페이지 수집 성공 시 처리 ---
                    successful_pages += 1

                    if not products:
                        consecutive_empty += 1
                        # 원칙 7: 48개 미만이라고 바로 종료하지 않음. 연속 2회 비어있을 때만 종료
                        if consecutive_empty >= 2:
                            logging.info(f"[{parent_name} > {sub_name}] 빈 페이지 2회 연속 감지 - 해당 카테고리 수집 정상 종료")
                            break
                        page_idx += 1
                        continue

                    consecutive_empty = 0
                    category_products.extend(products)
                    logging.info(
                        f"[{parent_name} > {sub_name}] page {page_idx}: "
                        f"{len(products)}개 수집 (누적 {len(category_products)}개)"
                    )

                    page_idx += 1

                    # 원칙 4: 페이지 사이 랜덤 대기 시간 적용 (1.5초 ~ 3.0초)
                    delay = random.uniform(config.REQUEST_DELAY_SECONDS, config.REQUEST_DELAY_SECONDS + 1.5)
                    time.sleep(delay)

                # 서브카테고리 완료 후 실패 페이지 기록 저장
                if sub_category_failed_pages:
                    current_run_failed[f"{parent_name}>{sub_name}"] = sub_category_failed_pages

                for p in category_products:
                    p["parent_category"] = parent_name
                    all_products[p["product_id"]] = p

    # 원칙 9: 이번 실행의 실패 페이지를 파일에 저장 (다음 실행 시 참조)
    # 기존 기록과 병합하여 최신화
    merged_failed = config.load_failed_pages()
    merged_failed.update(current_run_failed)
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

    logging.info("\n" + "=" * 50)
    logging.info("📊 올리브영 카탈로그 수집 최종 통계")
    logging.info(f"총 시도 페이지: {total_attempted}")
    logging.info(f"성공 페이지: {successful}")
    logging.info(f"실패 페이지: {failed_count}")
    logging.info(f"발견 상품(중복제외전): {len(products)}")
    logging.info(f"고유 상품 수: {unique_count}")

    if failed_count > 0:
        logging.warning("⚠️ 불완전 수집 감지: 일부 페이지 수집에 실패했습니다.")
        for cat, page, err in failed_details[:10]:  # 최대 10개만 로그 출력
            logging.warning(f"  - {cat} page {page}: {err}")
        if len(failed_details) > 10:
            logging.warning(f"  ... 외 {len(failed_details) - 10}개 페이지 실패")
    logging.info("=" * 50 + "\n")

    conn = db.connect()
    status = "SUCCESS"
    stats = {"NEW": 0, "CHANGED": 0, "UNCHANGED": 0}

    try:
        if unique_count < config.MIN_CATALOG_ITEMS:
            status = "FAILED_MIN_ITEMS"
            logging.error(
                f"수집량({unique_count})이 MIN_CATALOG_ITEMS({config.MIN_CATALOG_ITEMS}) 미만이라 "
                "상품 상태(ACTIVE/MISSING) 갱신은 건너뜁니다. (차단/HTML변경 오탐 방지)"
            )
        else:
            seen_ids = set()
            for p in products:
                db.upsert_product(conn, p, started_at)
                change_type = db.save_snapshot(conn, p, run_date, started_at)
                stats[change_type] += 1
                seen_ids.add(p["product_id"])

            logging.info(
                f"스냅샷 저장 통계: 신규 {stats['NEW']}개 / "
                f"변경 {stats['CHANGED']}개 / 미변경 {stats['UNCHANGED']}개"
            )
            # ✅ 텔레그램 알림이 안정적으로 grep할 수 있도록 고정 포맷 마커 로그 추가
            logging.info(f"METRIC OY_CATALOG_COLLECTED={unique_count}")
            logging.info(f"METRIC OY_CATALOG_NEW={stats['NEW']}")

            # 원칙 10: 불완전 수집 시 MISSING 처리 절대 금지
            if failed_count > 0:
                status = "PARTIAL_PAGE_FAILURE"
                logging.error(
                    "🚨 [중요] 수집이 불완전하므로 DB의 기존 상품을 MISSING으로 처리하지 않습니다. "
                    "다음 실행에서 실패 페이지 재시도가 완료될 때까지 기존 데이터는 보호됩니다."
                )
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
                "oliveyoung", run_date, started_at,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status,
                len(config.RANKING_CATEGORIES) - 1,  # surfaces
                successful,  # pages (성공한 페이지만 기록)
                len(products),
                unique_count,
            ),
        )
        conn.commit()

    except Exception as e:
        conn.rollback()
        logging.error(f"카탈로그 저장 중 치명적 오류: {e}")
        raise
    finally:
        conn.close()

    return products, status, stats

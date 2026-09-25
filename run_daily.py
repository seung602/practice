import json
import logging
import os
import sys
from datetime import datetime

import ranking_collector
import catalog_collector
import config
import daiso_catalog_collector
import translate_service
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def run_ranking_step():
    """일간 올리브영 랭킹 수집/저장. 실패하면 예외를 올려 워크플로우가 실패하도록 한다."""
    logging.info("=== [1/4] 올리브영 랭킹 수집 시작 ===")
    now = datetime.now()
    ranking_date = now.strftime("%Y-%m-%d")
    collected_at = now.strftime("%Y-%m-%d %H:%M:%S")

    rankings = ranking_collector.collect_rankings()
    if not rankings:
        raise RuntimeError("올리브영 랭킹 데이터가 0개입니다.")

    conn = db.connect()
    try:
        saved_count = ranking_collector.save_rankings(
            conn, rankings, "ALL", ranking_date, collected_at
        ) or 0
        if saved_count < getattr(config, 'MIN_RANKING_ITEMS', 80):
            raise RuntimeError(
                f"올리브영 랭킹 저장량이 비정상적으로 적습니다: {saved_count}개"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    os.makedirs("data", exist_ok=True)
    with open("data/oliveyoung_best.json", "w", encoding="utf-8") as f:
        json.dump(rankings, f, ensure_ascii=False, indent=2)

    logging.info(
        "=== [1/4] 랭킹 수집 완료: %s개 파싱 / %s개 저장 ===",
        len(rankings), saved_count,
    )
    logging.info("METRIC OY_RANKING_SAVED=%s", saved_count)
    return rankings


def run_catalog_step():
    """올리브영 전체 상품목록 수집. 비정상 수집이면 예외를 올린다."""
    logging.info("=== [2/4] 올리브영 전체 상품 카탈로그 수집 시작 ===")
    products, status, stats = catalog_collector.run_catalog_collection()
    if status != "SUCCESS":
        raise RuntimeError(f"올리브영 카탈로그 실패 상태: {status}")
    if not products:
        raise RuntimeError("올리브영 카탈로그 상품이 0개입니다.")

    bundle_count = sum(1 for p in products if p.get("is_bundle"))
    logging.info(
        "=== [2/4] 올리브영 카탈로그 완료: %s개 수집 (기획전/1+1 %s개, 신규 %s개) ===",
        len(products), bundle_count, stats.get("NEW", 0),
    )
    logging.info("METRIC OY_CATALOG_COLLECTED=%s", len(products))
    logging.info("METRIC OY_CATALOG_NEW=%s", stats.get("NEW", 0))
    return products


def run_daiso_catalog_step():
    """다이소몰 뷰티 전체상품 수집 및 자체 랭킹 계산."""
    logging.info("=== [3/4] 다이소몰 뷰티 카탈로그 수집 시작 ===")
    products, status, stats = daiso_catalog_collector.run_daiso_catalog_collection()
    logging.info("=== 다이소 카탈로그 수집 완료: %s개 (status=%s) ===", len(products), status)

    conn = db.connect()
    try:
        rows = conn.execute("""
            SELECT product_id, review_count, rating, product_name
            FROM products
            WHERE source='daiso' AND status='ACTIVE'
        """).fetchall()
        updated = 0
        for row in rows:
            p = {
                "product_id": row["product_id"],
                "review_count": row["review_count"],
                "rating": row["rating"],
            }
            score = db.compute_daiso_score(p)
            conn.execute(
                "UPDATE products SET daiso_score = ? WHERE product_id = ?",
                (score, row["product_id"]),
            )
            updated += 1
        conn.commit()
        logging.info("다이소 %s개 상품 점수 재계산 완료", updated)
        run_date = datetime.now().strftime("%Y-%m-%d")
        ranked_count = db.update_daiso_rankings(conn, run_date)
        logging.info("다이소 자체 랭킹 %s개 등록 완료", ranked_count)
        logging.info("METRIC DAISO_CATALOG_COLLECTED=%s", len(products))
        logging.info("METRIC DAISO_CATALOG_NEW=%s", stats.get("NEW", 0))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return products


def run_translation_step():
    logging.info("=== [4/4] 상품명 영어 번역 캐시 갱신 시작 ===")
    conn = db.connect()
    try:
        stats = translate_service.sync_translations(conn)
        logging.info("=== [4/4] 번역 캐시 갱신 완료: %s ===", stats)
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    errors = []
    for name, func in (
        ("올리브영 랭킹", run_ranking_step),
        ("올리브영 카탈로그", run_catalog_step),
        ("다이소 카탈로그", run_daiso_catalog_step),
        ("번역", run_translation_step),
    ):
        try:
            func()
        except Exception as exc:
            logging.exception("[%s] 실패: %s", name, exc)
            errors.append((name, str(exc)))
            # 나머지 단계는 계속 수행하되 마지막에 실패 코드로 종료한다.

    if errors:
        logging.error("=== 전체 실행 실패: %s개 단계 오류 ===", len(errors))
        for name, message in errors:
            logging.error(" - %s: %s", name, message)
        return 1

    logging.info("=== 전체 실행 정상 완료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

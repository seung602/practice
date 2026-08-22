import json
import logging
import os
from datetime import datetime

import ranking_collector
import catalog_collector
import daiso_catalog_collector
import translate_service
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def run_ranking_step():
    """일간 랭킹(Top100) 수집 -> DB 저장"""
    logging.info("=== [1/3] 올리브영 랭킹 수집 시작 ===")
    now = datetime.now()
    ranking_date = now.strftime("%Y-%m-%d")
    collected_at = now.strftime("%Y-%m-%d %H:%M:%S")

    try:
        rankings = ranking_collector.collect_rankings()
    except Exception as e:
        logging.error(f"랭킹 수집 실패: {e}")
        return []

    if not rankings:
        logging.warning("수집된 랭킹 데이터가 없습니다.")
        return []

    conn = db.connect()
    saved_count = 0
    try:
        # ✅ 기획전/1+1 포함 실제 올리브영 화면과 동일하게 전체 저장 (재수집 시 중복도 자동 정리됨)
        saved_count = ranking_collector.save_rankings(conn, rankings, "ALL", ranking_date, collected_at) or 0
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"랭킹 저장 실패: {e}")
    finally:
        conn.close()

    # 참고용 JSON 스냅샷도 함께 남김
    os.makedirs("data", exist_ok=True)
    with open("data/oliveyoung_best.json", "w", encoding="utf-8") as f:
        json.dump(rankings, f, ensure_ascii=False, indent=2)

    logging.info(f"=== [1/3] 랭킹 수집 완료: {len(rankings)}개 파싱 / {saved_count}개 저장(기획전 포함) ===")
    logging.info(f"METRIC OY_RANKING_SAVED={saved_count}")
    return rankings


def run_catalog_step():
    """올리브영 전체 상품목록 수집 -> DB 저장"""
    logging.info("=== [2/3] 올리브영 전체 상품 카탈로그 수집 시작 ===")
    try:
        products, status, stats = catalog_collector.run_catalog_collection()
    except Exception as e:
        logging.error(f"카탈로그 수집 실패: {e}")
        return []

    if products:
        bundle_count = sum(1 for p in products if p.get("is_bundle"))
        logging.info(
            f"=== [2/3] 올리브영 카탈로그 완료: {len(products)}개 수집 "
            f"(그 중 기획전/1+1 {bundle_count}개, 신규 {stats.get('NEW', 0)}개 — 랭킹에는 모두 포함됨) ==="
        )
    return products


def run_daiso_catalog_step():
    """다이소몰 뷰티 전체상품 수집 -> DB 저장 -> 🚨 자체 랭킹 계산"""
    logging.info("=== [3/3] 다이소몰 뷰티 카탈로그 수집 시작 ===")
    try:
        products, status, stats = daiso_catalog_collector.run_daiso_catalog_collection()
    except Exception as e:
        logging.error(f"다이소 카탈로그 수집 실패: {e}")
        return []

    logging.info(f"=== 다이소 카탈로그 수집 완료: {len(products)}개 (status={status}) ===")

    # 🚨🚨🚨 다이소 자체 랭킹 계산 및 저장 🚨🚨🚨
    # 수집기가 daiso_score를 못 넣어줘도 여기서 재계산하므로 안전
    logging.info("=== 다이소 자체 랭킹(CUSTOM_SCORE) 계산 시작 ===")
    try:
        conn = db.connect()
        try:
            # 1) 모든 다이소 ACTIVE 상품에 대해 daiso_score 재계산
            rows = conn.execute("""
                SELECT product_id, review_count, rating, product_name
                FROM products
                WHERE source='daiso' AND status='ACTIVE'
            """).fetchall()

            updated = 0
            for row in rows:
                # compute_daiso_score가 필요한 필드만 dict로 구성
                p = {
                    "product_id": row["product_id"],
                    "review_count": row["review_count"],
                    "rating": row["rating"],
                }
                score = db.compute_daiso_score(p)
                conn.execute(
                    "UPDATE products SET daiso_score = ? WHERE product_id = ?",
                    (score, row["product_id"])
                )
                updated += 1

            conn.commit()
            logging.info(f"✅ 다이소 {updated}개 상품 점수 재계산 완료")

            # 2) 점수 순으로 daily_rankings 테이블에 저장
            run_date = datetime.now().strftime("%Y-%m-%d")
            ranked_count = db.update_daiso_rankings(conn, run_date)
            logging.info(f"✅ 다이소 자체 랭킹 {ranked_count}개 등록 완료 (CUSTOM_SCORE 타입)")

        finally:
            conn.close()
    except Exception as e:
        logging.error(f"❌ 다이소 랭킹 계산 실패: {e}")

    return products


def run_translation_step():
    """신규/변경된 상품명만 Gemini로 영어 번역 -> product_name_en 캐시 갱신"""
    logging.info("=== [4/4] 상품명 영어 번역(Gemini) 캐시 갱신 시작 ===")
    conn = db.connect()
    try:
        stats = translate_service.sync_translations(conn)
        logging.info(f"=== [4/4] 번역 캐시 갱신 완료: {stats} ===")
        return stats
    except Exception as e:
        logging.error(f"번역 단계 실패(카탈로그 데이터에는 영향 없음): {e}")
        return None
    finally:
        conn.close()


def main():
    # 단계별로 독립 실행 -> 하나가 실패해도 나머지는 계속 진행
    run_ranking_step()
    run_catalog_step()
    run_daiso_catalog_step()
    run_translation_step()


if __name__ == "__main__":
    main()

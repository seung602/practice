import json
import logging
from datetime import datetime

import db
from oliveyoung_client import OliveYoungClient
from oliveyoung_parser import parse_ranked_products

get_db_conn_func = getattr(db, "get_db_connection", getattr(db, "get_connection", getattr(db, "connect_db", None)))
upsert_product = getattr(db, "upsert_product", None)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def save_rankings(conn, rankings, category, ranking_date, collected_at):
    if not rankings:
        logging.warning("[%s] 저장할 랭킹 데이터가 없습니다.", category)
        return 0
    if not upsert_product:
        logging.error("db.py 파일 내에 'upsert_product' 함수가 정의되어 있지 않습니다.")
        return 0

    if hasattr(db, "delete_oliveyoung_rankings"):
        db.delete_oliveyoung_rankings(conn, ranking_date, category)

    saved_count = 0
    for idx, item in enumerate(rankings):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                logging.error("[%s] %s번째 JSON 파싱 실패", category, idx + 1)
                continue
        if not isinstance(item, dict) or "product_id" not in item:
            logging.error("[%s] %s번째 항목 형식 오류", category, idx + 1)
            continue
        try:
            upsert_product(conn, item, collected_at)
            db.save_ranking(conn, item, "DAILY_BEST", category, ranking_date, collected_at)
            saved_count += 1
        except Exception as e:
            logging.error("[%s] 상품 저장 실패 (%s): %s", category, item.get("product_id"), e)

    logging.info("[%s] %s개 중 %s개 저장 완료.", category, len(rankings), saved_count)
    return saved_count


def collect_rankings():
    with OliveYoungClient() as client:
        html = client.fetch_top100()
    items = parse_ranked_products(html, category="ALL", limit=100)
    logging.info("[ALL] 랭킹 파싱 결과: %s개", len(items))
    return items


def main():
    now = datetime.now()
    ranking_date = now.strftime("%Y-%m-%d")
    collected_at = now.strftime("%Y-%m-%d %H:%M:%S")

    logging.info("=== 랭킹 수집 및 저장 시작 ===")
    if get_db_conn_func is None:
        logging.error("db.py의 DB 연결 함수를 찾지 못했습니다.")
        return

    rankings = collect_rankings()
    import config
    min_required = getattr(config, "MIN_RANKING_ITEMS", 80)
    if len(rankings) < min_required:
        raise RuntimeError(
            f"랭킹 수집량이 비정상적으로 적습니다: {len(rankings)}개 < {min_required}개. "
            "기존 당일 랭킹을 건드리지 않습니다."
        )

    conn = None
    try:
        conn = get_db_conn_func()
        saved = save_rankings(conn, rankings, "ALL", ranking_date, collected_at)
        if hasattr(conn, "commit"):
            conn.commit()
        logging.info("=== 랭킹 수집 정상 완료: %s개 파싱 / %s개 저장 ===", len(rankings), saved)
    except Exception:
        if conn and hasattr(conn, "rollback"):
            conn.rollback()
        raise
    finally:
        if conn and hasattr(conn, "close"):
            conn.close()


def run_ranking():
    main()


if __name__ == "__main__":
    main()

import json
import logging
from datetime import datetime

# 1. db.py 모듈 불러오기
import db
from oliveyoung_client import OliveYoungClient
from oliveyoung_parser import parse_ranked_products

# db.py 내부에 존재하는 연결 함수를 자동으로 찾아 할당 (없을 경우 예외 처리)
get_db_conn_func = getattr(db, 'get_db_connection', 
                   getattr(db, 'get_connection', 
                   getattr(db, 'connect_db', None)))

# upsert_product 함수 확인
upsert_product = getattr(db, 'upsert_product', None)

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def save_rankings(conn, rankings, category, ranking_date, collected_at):
    """
    수집된 랭킹 리스트를 DB에 안전하게 저장합니다.
    기획전/번들 상품(is_bundle) 여부와 상관없이 사이트에 노출된 순서대로 1~100위를 모두 저장합니다.
    """
    if not rankings:
        logging.warning(f"[{category}] 저장할 랭킹 데이터가 없습니다.")
        return

    if not upsert_product:
        logging.error("db.py 파일 내에 'upsert_product' 함수가 정의되어 있지 않습니다.")
        return

    saved_count = 0
    bundle_count = 0  # 기획전/번들 상품 카운트

    for idx, item in enumerate(rankings):
        # 1. item이 문자열 형태(JSON string)인 경우 dict로 파싱 시도
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                logging.error(f"[{category}] {idx+1}번째 항목이 문자열이지만 JSON 파싱 실패: {item}")
                continue

        # 2. dict 타입 검증
        if not isinstance(item, dict):
            logging.error(f"[{category}] {idx+1}번째 항목이 dict 타입이 아닙니다. (타입: {type(item)}, 내용: {item})")
            continue

        # 3. 필수 키 점검 (product_id 확인)
        if "product_id" not in item:
            logging.error(f"[{category}] {idx+1}번째 항목에 'product_id'가 없습니다: {item}")
            continue

        # 4. 기획전 상품 통계 수집 (DB 저장은 필터링 없이 그대로 진행)
        if item.get("is_bundle"):
            bundle_count += 1

        # 5. DB 저장 실행 (상품 마스터 upsert + 랭킹 원본 저장)
        try:
            upsert_product(conn, item, collected_at)
            db.save_ranking(conn, item, "DAILY_BEST", category, ranking_date, collected_at)
            saved_count += 1
        except Exception as e:
            logging.error(f"[{category}] 상품 저장 실패 (ID: {item.get('product_id')}): {e}")

    # ✅ 기획전 포함 여부를 로그로 명확히 출력 (디버깅 및 검증용)
    logging.info(
        f"[{category}] 총 {len(rankings)}개 중 {saved_count}개 저장 완료 "
        f"(이 중 기획전/번들 상품: {bundle_count}개 포함)"
    )


def collect_rankings():
    """
    랭킹 수집 크롤러 logic.
    getBestList.do(일간 랭킹 100위) 페이지를 수집해 파싱합니다.
    """
    with OliveYoungClient() as client:
        html = client.fetch_top100()  # 👈 일간 베스트 100위 수집
    items = parse_ranked_products(html, category="ALL", limit=100)
    return items


def main():
    """
    실행 메인 로직
    """
    now = datetime.now()
    ranking_date = now.strftime("%Y-%m-%d")
    collected_at = now.strftime("%Y-%m-%d %H:%M:%S")

    # GitHub Actions 로그 포맷과 일치하도록 [1/3] 태그 사용
    logging.info("=== [1/3] 올리브영 랭킹 수집 시작 ===")

    # DB 연결 함수 존재 여부 확인
    if get_db_conn_func is None:
        logging.error("db.py 파일에서 DB 연결 함수(get_db_connection, get_connection 등)를 찾을 수 없습니다.")
        return

    # 1. 데이터 수집
    rankings = collect_rankings()

    if not rankings:
        logging.warning("수집된 데이터가 없어 저장 프로세스를 종료합니다.")
        return

    # 2. DB 연결 및 저장
    conn = None
    try:
        conn = get_db_conn_func()
        save_rankings(conn, rankings, "ALL", ranking_date, collected_at)
        
        # 커밋 메서드가 존재하는 DB 연결인 경우 수행
        if hasattr(conn, 'commit'):
            conn.commit()
            
        # ✅ 최종 완료 로그 (기획전 포함 100개 유지 강조)
        logging.info(f"=== [1/3] 랭킹 수집 완료: {len(rankings)}개 (기획전/번들 상품 포함, 순위 밀림 없음) ===")
    except Exception as e:
        if conn and hasattr(conn, 'rollback'):
            conn.rollback()
        logging.error(f"랭킹 수집 처리 중 오류 발생: {e}")
    finally:
        if conn and hasattr(conn, 'close'):
            conn.close()


def run_ranking():
    """
    run_daily.py 등 외부 스크립트에서 호출하는 엔트리 포인트
    """
    main()


if __name__ == "__main__":
    main()

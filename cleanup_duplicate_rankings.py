"""
✅ 1회성 정리 스크립트: daily_rankings 테이블에 이미 쌓인 중복 행을 정리한다.

배경:
  - 과거에는 db.py의 랭킹 저장 함수가 같은 run_date에 대해 기존 행을 지우지 않고
    매번 INSERT만 했기 때문에, cron-job.org 등 외부 트리거로 하루에 여러 번
    수집이 실행되면 같은 (source, run_date, product_id) 조합이 여러 번 저장되어
    daily_rankings에 중복 행이 쌓여 있다.
  - 이제 db.py는 저장 전에 기존 당일 데이터를 삭제하도록 고쳤지만(향후 재발 방지),
    "이미 쌓여있는" 과거 중복 데이터는 이 스크립트로 한 번 정리해야 한다.

동작:
  - (source, run_date, product_id) 조합별로 가장 최근에 captured_at 된 행 1개만 남기고 나머지를 삭제.
  - 실제 삭제 전에 --dry-run으로 몇 개가 지워질지 먼저 확인 가능.

사용법:
  python cleanup_duplicate_rankings.py --dry-run   # 몇 개가 중복인지만 확인
  python cleanup_duplicate_rankings.py              # 실제로 삭제 수행
"""
import argparse
import sqlite3

import config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="삭제하지 않고 몇 개가 중복인지만 출력")
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    # 중복 판단 기준: 같은 (source, run_date, product_id) 조합 중 id가 가장 큰(=가장 나중에 저장된) 것만 남긴다.
    dup_rows = conn.execute("""
        SELECT source, run_date, product_id, COUNT(*) as cnt
        FROM daily_rankings
        GROUP BY source, run_date, product_id
        HAVING COUNT(*) > 1
    """).fetchall()

    total_dup_extra = sum(r["cnt"] - 1 for r in dup_rows)
    print(f"중복 그룹 수: {len(dup_rows)}개 / 삭제 대상 행 수: {total_dup_extra}개")

    if args.dry_run:
        print("(--dry-run 이므로 실제 삭제는 수행하지 않았습니다)")
        conn.close()
        return

    if total_dup_extra == 0:
        print("중복 데이터가 없습니다. 종료합니다.")
        conn.close()
        return

    conn.execute("""
        DELETE FROM daily_rankings
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM daily_rankings
            GROUP BY source, run_date, product_id
        )
    """)
    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) c FROM daily_rankings").fetchone()["c"]
    print(f"✅ 중복 {total_dup_extra}개 삭제 완료. 정리 후 daily_rankings 총 {remaining}개 행 남음.")
    conn.close()


if __name__ == "__main__":
    main()

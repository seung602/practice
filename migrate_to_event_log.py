#!/usr/bin/env python3
"""
product_snapshots 기반 구조에서 event log 기반으로 안전하게 전환하는 마이그레이션 스크립트.

기능:
1. DB 파일 백업
2. product_events 테이블 생성
3. products 테이블에 current_price, current_sale_price 컬럼 추가
4. product_snapshots 중복 정리
5. 상품별 최신 snapshot 값을 products.current_price/current_sale_price로 이관
6. 최초 BASELINE 이벤트 기록
7. 원하면 --delete-snapshots 옵션으로 snapshot 테이블 비움

사용법:
    python migrate_to_event_log.py
    python migrate_to_event_log.py --db path/to/your.db
    python migrate_to_event_log.py --delete-snapshots --vacuum
"""

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime

try:
    from config import DB_PATH as DEFAULT_DB_PATH
except Exception:
    DEFAULT_DB_PATH = "beauty_catalog.db"


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            detected_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(product_id)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_product_events_product_time
        ON product_events(product_id, detected_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_product_events_type_time
        ON product_events(event_type, detected_at)
    """)

    # 기존 products 테이블에 컬럼 추가
    try:
        conn.execute("ALTER TABLE products ADD COLUMN current_price INTEGER")
    except Exception:
        pass

    try:
        conn.execute("ALTER TABLE products ADD COLUMN current_sale_price INTEGER")
    except Exception:
        pass

    conn.commit()


def deduplicate_snapshots(conn):
    """같은 (snapshot_date, product_id) 내에서 가장 큰 id 하나만 남기고 삭제."""
    if not table_exists(conn, "product_snapshots"):
        return 0

    before = conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0]

    conn.execute("""
        DELETE FROM product_snapshots
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM product_snapshots
            GROUP BY snapshot_date, product_id
        )
    """)
    conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0]
    return before - after


def migrate_latest_snapshot_to_products(conn):
    """상품별 최신 snapshot의 price/sale_price를 products.current_* 로 이관."""
    if not table_exists(conn, "product_snapshots"):
        return 0

    cur = conn.cursor()

    cur.execute("""
        SELECT s.product_id,
               s.price,
               s.sale_price,
               s.collected_at
        FROM product_snapshots s
        JOIN (
            SELECT product_id,
                   MAX(snapshot_date) AS max_date
            FROM product_snapshots
            GROUP BY product_id
        ) m
          ON s.product_id = m.product_id
         AND s.snapshot_date = m.max_date
        WHERE s.id = (
            SELECT MAX(s2.id)
            FROM product_snapshots s2
            WHERE s2.product_id = s.product_id
              AND s2.snapshot_date = m.max_date
        )
    """)

    rows = cur.fetchall()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    migrated = 0

    for product_id, price, sale_price, collected_at in rows:
        detected_at = collected_at or now

        cur.execute(
            """
            UPDATE products
            SET current_price = ?,
                current_sale_price = ?
            WHERE product_id = ?
            """,
            (price, sale_price, product_id)
        )

        # 아직 이벤트가 하나도 없는 상품이면 BASELINE 이벤트 1개만 심음
        exists_event = cur.execute(
            "SELECT 1 FROM product_events WHERE product_id = ? LIMIT 1",
            (product_id,)
        ).fetchone()

        if not exists_event:
            payload = {
                "price": price,
                "sale_price": sale_price,
            }
            cur.execute(
                """
                INSERT INTO product_events (
                    product_id,
                    event_type,
                    old_value,
                    new_value,
                    detected_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    "BASELINE",
                    None,
                    json.dumps(payload, ensure_ascii=False),
                    detected_at,
                )
            )

        migrated += 1

    conn.commit()
    return migrated


def print_report(conn):
    products_count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    events_count = conn.execute("SELECT COUNT(*) FROM product_events").fetchone()[0]

    null_price_count = conn.execute(
        "SELECT COUNT(*) FROM products WHERE current_price IS NULL"
    ).fetchone()[0]

    print("\n=== 마이그레이션 리포트 ===")
    print(f"products rows: {products_count}")
    print(f"product_events rows: {events_count}")
    print(f"products.current_price NULL rows: {null_price_count}")

    if table_exists(conn, "product_snapshots"):
        snapshots_count = conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0]
        print(f"product_snapshots rows: {snapshots_count}")
    else:
        print("product_snapshots table not found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB file path")
    parser.add_argument("--delete-snapshots", action="store_true",
                        help="마이그레이션 후 product_snapshots 테이블을 비웁니다.")
    parser.add_argument("--vacuum", action="store_true",
                        help="DB 용량 축소를 위해 VACUUM을 실행합니다.")
    args = parser.parse_args()

    db_path = args.db

    if not os.path.exists(db_path):
        raise SystemExit(f"DB 파일이 없습니다: {db_path}")

    backup_path = f"{db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db_path, backup_path)
    print(f"백업 생성 완료: {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        ensure_schema(conn)

        removed_duplicates = deduplicate_snapshots(conn)
        print(f"product_snapshots 중복 행 삭제: {removed_duplicates} rows")

        migrated = migrate_latest_snapshot_to_products(conn)
        print(f"최신 snapshot -> products 이관 완료: {migrated} products")

        print_report(conn)

        if args.delete_snapshots:
            if table_exists(conn, "product_snapshots"):
                print("\nproduct_snapshots 테이블을 비웁니다...")
                conn.execute("DELETE FROM product_snapshots")
                conn.commit()
                print("product_snapshots 삭제 완료")
            else:
                print("product_snapshots 테이블이 없습니다.")

        if args.vacuum:
            print("VACUUM 실행 중...")
            conn.execute("VACUUM")
            print("VACUUM 완료")

        print("\n완료되었습니다.")
        print("다음 단계: 코드 수정 후 Actions를 1회 실행하고 product_events를 확인하세요.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()

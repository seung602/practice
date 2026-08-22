import sqlite3
import logging
import math
import os
import config

logger = logging.getLogger(__name__)


def connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    init_schema(conn)
    return conn


def init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY, source TEXT, brand TEXT, product_name TEXT,
            product_url TEXT, category TEXT, parent_category TEXT,
            first_seen_at TEXT, last_catalog_seen_at TEXT, status TEXT DEFAULT 'ACTIVE',
            suspected_missing_since TEXT, is_new INTEGER DEFAULT 0,
            price INTEGER, sale_price INTEGER, is_bundle INTEGER DEFAULT 0,
            review_count INTEGER, rating REAL, daiso_score REAL DEFAULT 0,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS product_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT, captured_at TEXT,
            source TEXT, brand TEXT, product_name TEXT, product_url TEXT,
            category TEXT, parent_category TEXT, price INTEGER, sale_price INTEGER,
            status TEXT, is_bundle INTEGER DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
        CREATE TABLE IF NOT EXISTS daily_rankings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, run_date TEXT,
            category TEXT, rank_num INTEGER, product_id TEXT, brand TEXT,
            product_name TEXT, product_url TEXT, price INTEGER, sale_price INTEGER,
            captured_at TEXT, is_bundle INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ranking_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, category TEXT,
            product_id TEXT, current_rank INTEGER, previous_rank INTEGER,
            change_amount INTEGER, change_type TEXT
        );
        CREATE TABLE IF NOT EXISTS catalog_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, run_date TEXT,
            started_at TEXT, finished_at TEXT, status TEXT,
            surfaces INTEGER, pages INTEGER, items_found INTEGER, unique_products INTEGER
        );
    """)

    cursor = conn.execute("PRAGMA table_info(products)")
    existing = {row[1] for row in cursor.fetchall()}
    for col, typ in [
        ('suspected_missing_since', 'TEXT'),
        ('is_new', 'INTEGER DEFAULT 0'),
        ('parent_category', 'TEXT'),
        ('price', 'INTEGER'),
        ('sale_price', 'INTEGER'),
        ('is_bundle', 'INTEGER DEFAULT 0'),
        ('review_count', 'INTEGER'),
        ('rating', 'REAL'),
        ('daiso_score', 'REAL DEFAULT 0'),
        ('created_at', 'TEXT'),
        ('updated_at', 'TEXT'),
        ('product_name_en', 'TEXT'),
        ('name_en_hash', 'TEXT'),
        ('brand_en', 'TEXT'),
        ('brand_en_hash', 'TEXT'),
        ('category_en', 'TEXT'),
        ('category_en_hash', 'TEXT'),
        ('parent_category_en', 'TEXT'),
        ('parent_category_en_hash', 'TEXT'),
    ]:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")
                logger.info(f"✅ products에 {col} 추가됨")
            except Exception as e:
                logger.warning(f"{col} 추가 실패: {e}")

    try:
        conn.execute("UPDATE products SET created_at = datetime('now') WHERE created_at IS NULL")
        conn.execute("UPDATE products SET updated_at = datetime('now') WHERE updated_at IS NULL")
    except Exception as e:
        logger.warning(f"created_at/updated_at 업데이트 실패: {e}")

    cursor_snap = conn.execute("PRAGMA table_info(product_snapshots)")
    existing_snap = {row[1] for row in cursor_snap.fetchall()}
    for col, typ in [
        ('captured_at', 'TEXT'),
        ('source', 'TEXT'),
        ('brand', 'TEXT'),
        ('product_name', 'TEXT'),
        ('product_url', 'TEXT'),
        ('category', 'TEXT'),
        ('parent_category', 'TEXT'),
        ('price', 'INTEGER'),
        ('sale_price', 'INTEGER'),
        ('status', 'TEXT'),
        ('is_bundle', 'INTEGER DEFAULT 0'),
    ]:
        if col not in existing_snap:
            try:
                conn.execute(f"ALTER TABLE product_snapshots ADD COLUMN {col} {typ}")
            except Exception as e:
                pass

    cursor_rank = conn.execute("PRAGMA table_info(daily_rankings)")
    existing_rank = {row[1] for row in cursor_rank.fetchall()}
    if 'is_bundle' not in existing_rank:
        try:
            conn.execute("ALTER TABLE daily_rankings ADD COLUMN is_bundle INTEGER DEFAULT 0")
        except Exception:
            pass

    conn.commit()


def upsert_product(conn, p, captured_at):
    conn.execute("""
        INSERT INTO products (
            product_id, source, brand, product_name, product_url, category, parent_category,
            first_seen_at, last_catalog_seen_at, status, suspected_missing_since, is_new,
            price, sale_price, is_bundle, review_count, rating, daiso_score,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', NULL, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            brand = COALESCE(excluded.brand, products.brand),
            product_name = COALESCE(excluded.product_name, products.product_name),
            product_url = COALESCE(excluded.product_url, products.product_url),
            category = COALESCE(excluded.category, products.category),
            parent_category = COALESCE(excluded.parent_category, products.parent_category),
            last_catalog_seen_at = excluded.last_catalog_seen_at,
            status = 'ACTIVE',
            suspected_missing_since = NULL,
            price = COALESCE(excluded.price, products.price),
            sale_price = COALESCE(excluded.sale_price, products.sale_price),
            is_bundle = excluded.is_bundle,
            review_count = COALESCE(excluded.review_count, products.review_count),
            rating = COALESCE(excluded.rating, products.rating),
            daiso_score = COALESCE(excluded.daiso_score, products.daiso_score),
            updated_at = excluded.updated_at,
            is_new = CASE WHEN date(products.first_seen_at) = date(excluded.last_catalog_seen_at)
                          THEN 1 ELSE 0 END
    """, (
        p.get("product_id"), p.get("source"), p.get("brand"),
        p.get("product_name"), p.get("product_url"), p.get("category"), p.get("parent_category"),
        captured_at, captured_at,
        p.get("price"), p.get("sale_price"), 1 if p.get("is_bundle") else 0,
        p.get("review_count"), p.get("rating"), p.get("daiso_score"),
        captured_at, captured_at,
    ))


def save_snapshot(conn, p, run_date, captured_at):
    cursor = conn.execute("""
        SELECT price, sale_price, product_name
        FROM product_snapshots WHERE product_id = ? ORDER BY id DESC LIMIT 1
    """, (p.get("product_id"),))
    last_snap = cursor.fetchone()
    change_type = "UNCHANGED"
    if last_snap is None:
        change_type = "NEW"
    else:
        old_price, old_sale, old_name = last_snap["price"], last_snap["sale_price"], last_snap["product_name"]
        if old_price != p.get("price") or old_sale != p.get("sale_price") or old_name != p.get("product_name"):
            change_type = "CHANGED"

    conn.execute("""
        INSERT INTO product_snapshots (
            product_id, captured_at, source, brand, product_name, product_url,
            category, parent_category, price, sale_price, status, is_bundle
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
    """, (
        p.get("product_id"), captured_at, p.get("source"), p.get("brand"),
        p.get("product_name"), p.get("product_url"), p.get("category"),
        p.get("parent_category"), p.get("price"), p.get("sale_price"),
        1 if p.get("is_bundle") else 0,
    ))
    return change_type


def mark_catalog_missing(conn, seen_ids, run_date, source="oliveyoung"):
    cursor = conn.execute("""
        SELECT product_id, status FROM products
        WHERE status = 'ACTIVE' AND source = ?
    """, (source,))
    missing_count = 0
    for row in cursor.fetchall():
        pid = row["product_id"]
        if pid not in seen_ids:
            missing_count += 1
            conn.execute("""
                UPDATE products SET status = 'MISSING', suspected_missing_since = ?
                WHERE product_id = ?
            """, (run_date, pid))
    if missing_count > 0:
        logger.info(f"⚠️ 누락된 {source} 상품 {missing_count}개 MISSING 처리")


def update_missing_status_transitions(conn, run_date, source=None):
    """
    MISSING 상태인 상품을 suspected_missing_since 기준 경과일수에 따라
    SUSPECTED_INACTIVE(config.MISSING_DAYS_TO_SUSPECT일 이상) /
    INACTIVE(config.MISSING_DAYS_TO_INACTIVE일 이상)로 전이시킨다.

    README에는 이 전이 규칙이 문서화되어 있었지만 기존 코드에는 구현이 없어서
    한 번 MISSING이 된 상품이 영원히 MISSING 상태로 남아있던 문제를 해결한다.
    """
    where_source = ""
    params = []
    if source:
        where_source = "AND source = ?"
        params.append(source)

    cursor = conn.execute(
        f"""
        SELECT product_id, suspected_missing_since, status
        FROM products
        WHERE status IN ('MISSING', 'SUSPECTED_INACTIVE')
        AND suspected_missing_since IS NOT NULL
        {where_source}
        """,
        params,
    )

    from datetime import datetime as _dt

    try:
        run_dt = _dt.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        logger.warning(f"run_date 파싱 실패로 상태 전이를 건너뜁니다: {run_date}")
        return {"SUSPECTED_INACTIVE": 0, "INACTIVE": 0}

    suspect_count = 0
    inactive_count = 0

    for row in cursor.fetchall():
        pid = row["product_id"]
        since_raw = row["suspected_missing_since"]
        current_status = row["status"]

        try:
            since_dt = _dt.strptime(since_raw[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        days_missing = (run_dt - since_dt).days
        if days_missing < 0:
            continue

        new_status = None
        if days_missing >= config.MISSING_DAYS_TO_INACTIVE:
            new_status = "INACTIVE"
        elif days_missing >= config.MISSING_DAYS_TO_SUSPECT:
            new_status = "SUSPECTED_INACTIVE"

        if new_status and new_status != current_status:
            conn.execute(
                "UPDATE products SET status = ? WHERE product_id = ?",
                (new_status, pid),
            )
            if new_status == "SUSPECTED_INACTIVE":
                suspect_count += 1
            elif new_status == "INACTIVE":
                inactive_count += 1

    if suspect_count or inactive_count:
        logger.info(
            f"⏳ 상태 전이: SUSPECTED_INACTIVE {suspect_count}개, INACTIVE {inactive_count}개"
        )

    return {"SUSPECTED_INACTIVE": suspect_count, "INACTIVE": inactive_count}


def compute_daiso_score(p):
    score = 0.0
    review = p.get("review_count") or 0
    if review > 0:
        score += min(60.0, math.log10(review + 1) * 15)
    rating = p.get("rating") or 0
    if rating > 0:
        try:
            score += (min(float(rating), 5.0) / 5.0) * 20
        except Exception:
            pass
    if p.get("is_new"):
        score += 10.0
    if p.get("is_best") or p.get("is_popular"):
        score += 20.0
    return round(score, 2)


def delete_oliveyoung_rankings(conn, run_date, category="ALL"):
    """✅ 신규: 같은 날짜에 랭킹 수집이 여러 번 트리거되어도(cron-job.org 등 외부 트리거 중복 실행)
    daily_rankings에 중복 행이 쌓이지 않도록, 새로 저장하기 전에 같은 run_date의 기존 행을 지운다."""
    cur = conn.execute(
        "DELETE FROM daily_rankings WHERE source='oliveyoung' AND run_date=? AND category=?",
        (run_date, category),
    )
    if cur.rowcount:
        logger.info(f"🧹 올리브영 랭킹 재수집 감지: 기존 {run_date}({category}) {cur.rowcount}개 삭제 후 재저장")
    return cur.rowcount


def save_oliveyoung_rankings(conn, products, run_date, captured_at, category="ALL",
                              limit=100, commit=True, log_result=True):
    """
    ✅ 실제 올리브영 화면과 동일하게, 기획전/1+1 상품도 그대로 포함해서 저장한다.
    (이전에는 is_bundle이면 통째로 건너뛰어서 1,2,3,5위처럼 순위에 구멍이 뚫렸었음)
    is_bundle 여부 자체는 컬럼에 그대로 남겨서, 필요하면 나중에 화면에서만 뱃지로 표시 가능.
    """
    count = 0
    for p in products[:limit]:
        conn.execute("""
            INSERT INTO daily_rankings (
                source, run_date, category, rank_num, product_id, brand,
                product_name, product_url, price, sale_price, captured_at, is_bundle
            ) VALUES ('oliveyoung', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_date, category, p.get("rank"), p.get("product_id"), p.get("brand"),
            p.get("product_name"), p.get("product_url"), p.get("price"),
            p.get("sale_price"), captured_at, 1 if p.get("is_bundle") else 0,
        ))
        count += 1
    if commit:
        conn.commit()
    if log_result:
        logger.info(f"✅ 올리브영 랭킹 저장 완료: {count}개 (category={category})")
    return count


def update_daiso_rankings(conn, run_date, limit=100):
    # ✅ 같은 날 여러 번 실행돼도 중복 저장되지 않도록 기존 당일 랭킹 삭제 후 재저장
    cur = conn.execute("DELETE FROM daily_rankings WHERE source='daiso' AND run_date=? AND category='ALL'", (run_date,))
    if cur.rowcount:
        logger.info(f"🧹 다이소 랭킹 재수집 감지: 기존 {run_date} {cur.rowcount}개 삭제 후 재저장")

    rows = conn.execute("""
        SELECT product_id FROM products
        WHERE source = 'daiso' AND status = 'ACTIVE' AND daiso_score > 0
        ORDER BY daiso_score DESC, review_count DESC, product_name ASC
    """).fetchall()
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for i, row in enumerate(rows[:limit], start=1):
        conn.execute("""
            INSERT INTO daily_rankings (source, run_date, category, rank_num, product_id, captured_at)
            VALUES ('daiso', ?, 'ALL', ?, ?, ?)
        """, (run_date, i, row["product_id"], now))
    conn.commit()
    logger.info(f"✅ 다이소 자체 랭킹 갱신: {min(len(rows), limit)}개")
    return min(len(rows), limit)


# 🚨 레거시 랭킹 수집기 호환용 별칭 함수
def save_ranking(*args, **kwargs):
    """
    레거시 랭킹 수집기 호환용 (유연한 인자 처리).
    ranking_collector.save_rankings()에서 항목마다 1개씩 호출됨:
        db.save_ranking(conn, item, "DAILY_BEST", category, run_date, captured_at)

    ⚠️ 이전에는 호출될 때마다 매번 conn.commit()과 "1개 저장 완료" 로그를 찍어서
    랭킹 100개 저장 시 커밋 100번 + 의미 없는 로그 100줄이 발생했음.
    호출부(ranking_collector.save_rankings)가 어차피 끝나고 나서 요약 로그를
    남기고, run_daily.py에서 한 번에 commit()하므로 여기서는 commit/log 없이
    INSERT만 수행한다.
    """
    if len(args) >= 4:
        conn = args[0]

        # 인자 1이 dict(product)인지 str(product_id)인지 확인
        p = args[1] if isinstance(args[1], dict) else {"product_id": args[1]}

        # rank 인자 처리 (숫자면 rank로 취급, 문자열이면 category로 취급)
        if "rank" not in p and len(args) > 2 and isinstance(args[2], int):
            p["rank"] = args[2]

        # category 추출: 인자 3이 문자열이면 category로 사용, 아니면 기본값 'ALL'
        category = args[3] if len(args) > 3 and isinstance(args[3], str) else "ALL"

        # run_date와 captured_at 추출 (보통 마지막 두 인자)
        run_date = args[-2] if len(args) >= 5 else ""
        captured_at = args[-1] if len(args) >= 6 else ""

        return save_oliveyoung_rankings(
            conn, [p], run_date, captured_at, category=category, limit=100,
            commit=False, log_result=False,
        )
    return 0

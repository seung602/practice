"""
Gemini API를 이용해 product_name / brand / category / parent_category 를
영어로 번역하고 DB에 캐시로 저장한다.

캐시 동작 방식:
  - products.product_name_en / name_en_hash
  - products.brand_en / brand_en_hash
  - products.category_en / category_en_hash
  - products.parent_category_en / parent_category_en_hash
  - 매 실행 시 원문의 현재 해시와 저장된 해시를 비교해서
    바뀌지 않은 값은 API를 다시 호출하지 않고 건너뛴다.
  - 브랜드·카테고리는 중복이 많으므로 고유값만 모아 한 번 번역한 뒤
    같은 원문을 가진 모든 상품 행에 일괄 반영한다.

필요 환경변수:
  GEMINI_API_KEY   (필수) - 없으면 번역 단계 자체를 건너뜀
  GEMINI_MODEL     (선택, 기본 config.GEMINI_MODEL)
  TRANSLATE_ENABLED / TRANSLATE_BATCH_SIZE / TRANSLATE_MAX_PER_RUN (선택)
"""
import hashlib
import json
import logging
import time

import config

logger = logging.getLogger(__name__)


def _hash(name):
    return hashlib.md5((name or "").encode("utf-8")).hexdigest()


def _get_client():
    from google import genai  # 지연 import: GEMINI_API_KEY 없을 때 패키지 없어도 전체 파이프라인은 죽지 않게
    return genai.Client(api_key=config.GEMINI_API_KEY)


# ── 프롬프트 ──────────────────────────────────────────────────────────────

PROMPT_PRODUCT_NAME = """You will translate Korean cosmetics/beauty e-commerce product names into natural, fluent English,
as they would actually appear on an English-language beauty retail site (like Yesstyle or Amazon).

Input is a JSON array of Korean product name strings, in order.

Rules:
- Output ONLY a JSON array of strings, same length and same order as the input. No explanation, no markdown fences.
- Brand names: if you recognize the brand's real official English/romanized name, use it exactly
  (e.g. 메디힐 -> Mediheal, 토리든 -> Torriden, 바이오던스 -> Biodance, 리쥬란 -> Rejuran, 다이소 -> Daiso).
  If you do NOT recognize a brand, use standard Revised Romanization of Korean (RR) for just that brand
  token — do not guess an invented spelling.
- 🚨 CRITICAL — never syllable-by-syllable transliterate ordinary Korean WORDS or PHRASES that are not brand
  names. Every non-brand word must be genuinely TRANSLATED by meaning, not sounded out. For example:
    - "먹는" (edible/oral) -> "Edible" / "Oral", NEVER "Meokneun"
    - "이너닷" (inner + dot, a product line concept) -> translate the *concept* (e.g. "Inner Dot" / "Inner Glow"),
      NEVER a phonetic guess like "Ineodat"
    - "택1" / "택일" (choose one of N) -> "Pick 1" / "Choose 1", NEVER "Taek1"
    - "일분" as in "16일분" (a N-day supply) -> "16-Day Supply", NEVER "16Ilbun"
    - "듀오"/"세트"/"기획" -> "Duo"/"Set"/"Special Set", never left in Korean-sounding romanization
  If a word or short marketing phrase is ambiguous or you are not fully sure of its meaning, prefer a short
  natural English paraphrase of its likely meaning over any kind of sound-based guess. A sound-alike
  romanization of a non-brand word is always wrong output, even as a fallback.
- Translate ingredient names, product types, and marketing phrases into natural English used on beauty
  e-commerce sites.
- Keep numbers, units (ml, g, %), and bracket/parenthesis promo text, but translate the text inside them too.
- Keep it concise, like an actual English product listing title.

Input:
{items}
"""

PROMPT_BRAND = """You will translate Korean cosmetics/beauty brand names into their official English or commonly used romanized names.

Input is a JSON array of Korean (or mixed) brand name strings, in order.

Rules:
- Output ONLY a JSON array of strings, same length and same order as the input. No explanation, no markdown fences.
- If you recognize the brand's real official English/romanized name, use it exactly
  (e.g. 메디힐 -> Mediheal, 토리든 -> Torriden, 바이오던스 -> Biodance, 리쥬란 -> Rejuran,
   라운드랩 -> Round Lab, 닥터지 -> Dr.G, 아누아 -> Anua, 스킨푸드 -> Skin Food,
   필리밀리 -> Fillimilli, 피카소 -> Picasso, 식물나라 -> Nature Republic,
   유리아쥬 -> Uriage, 바이오더마 -> Bioderma, 다슈 -> Dashu, 더툴랩 -> The Tool Lab,
   올리브영 -> Olive Young, 다이소 -> Daiso).
- If you do NOT recognize a brand, use standard Revised Romanization of Korean (RR).
  Do not invent spellings. Do not translate meaning of brand names.
- Keep already-English or already-romanized brand names as-is (minor capitalization fixes OK).
- Keep it short — brand name only, no extra words.

Input:
{items}
"""

PROMPT_CATEGORY = """You will translate Korean cosmetics/beauty category names into natural English category labels
as used on English beauty retail sites (Yesstyle, Sephora, Amazon Beauty, etc.).

Input is a JSON array of Korean category strings, in order.

Rules:
- Output ONLY a JSON array of strings, same length and same order as the input. No explanation, no markdown fences.
- Translate by meaning into concise English category names. Examples:
    에센스/세럼/앰플 -> Essence / Serum / Ampoule
    크림 -> Cream
    마스크팩 -> Mask Pack
    시트팩 -> Sheet Mask
    선크림 -> Sunscreen
    클렌징폼/젤 -> Cleansing Foam / Gel
    스킨/토너 -> Toner
    바디로션/크림 -> Body Lotion / Cream
    핸드케어 -> Hand Care
    헤어기기/브러시 -> Hair Tools / Brush
    제모/왁싱 -> Hair Removal / Waxing
    샤워/입욕 -> Shower / Bath
    트리트먼트/팩 -> Treatment / Pack
    메이크업 툴 -> Makeup Tools
    스킨케어 -> Skincare
    바디케어 -> Body Care
    헤어케어 -> Hair Care
    뷰티소품 -> Beauty Tools
    클렌징 -> Cleansing
    선케어 -> Sun Care
    더모 코스메틱 -> Dermocosmetics
- Use Title Case. Keep slash-separated multi-categories as "A / B" form.
- Do not transliterate syllable-by-syllable. Always translate meaning.

Input:
{items}
"""


def _translate_batch(names, prompt_template):
    client = _get_client()
    prompt = prompt_template.format(items=json.dumps(names, ensure_ascii=False))
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    text = (resp.text or "").strip()
    try:
        out = json.loads(text)
    except Exception as e:
        logger.error(f"Gemini 응답 JSON 파싱 실패: {e} / raw={text[:300]}")
        return None
    if not isinstance(out, list) or len(out) != len(names):
        logger.error(f"Gemini 응답 길이 불일치: expected {len(names)}, got {type(out)} len={len(out) if isinstance(out, list) else 'N/A'}")
        return None
    return [str(x) for x in out]


def _run_batches(todo_pairs, prompt_template, label):
    """
    todo_pairs: list of (key, korean_text)
    returns: dict key -> english_text  (실패한 키는 포함하지 않음)
    """
    if not todo_pairs:
        return {}

    batch_size = max(1, config.TRANSLATE_BATCH_SIZE)
    result_map = {}
    failed = 0

    for i in range(0, len(todo_pairs), batch_size):
        batch = todo_pairs[i:i + batch_size]
        texts = [b[1] for b in batch]

        translated = None
        for attempt in range(3):
            try:
                translated = _translate_batch(texts, prompt_template)
                if translated:
                    break
            except Exception as e:
                logger.warning(f"🌐 [{label}] 배치 실패 (시도 {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))

        if not translated:
            failed += len(batch)
            logger.error(f"🌐 [{label}] 배치 최종 실패, {len(batch)}개 건너뜀")
            continue

        for (key, _), en in zip(batch, translated):
            result_map[key] = en

        logger.info(f"🌐 [{label}] 진행: {min(i + batch_size, len(todo_pairs))}/{len(todo_pairs)}")
        time.sleep(0.5)

    if failed:
        logger.warning(f"🌐 [{label}] 실패 {failed}개 (다음 실행에서 재시도)")
    return result_map


# ── 필드별 동기화 ─────────────────────────────────────────────────────────

def _sync_product_names(conn, force_all=False, max_items=None):
    rows = conn.execute(
        "SELECT product_id, product_name, product_name_en, name_en_hash "
        "FROM products WHERE status='ACTIVE' AND product_name IS NOT NULL AND product_name != ''"
    ).fetchall()

    todo, cached = [], 0
    for r in rows:
        h = _hash(r["product_name"])
        if not force_all and r["product_name_en"] and r["name_en_hash"] == h:
            cached += 1
            continue
        todo.append((r["product_id"], r["product_name"], h))

    limit = max_items if max_items is not None else (len(todo) if force_all else config.TRANSLATE_MAX_PER_RUN)
    if len(todo) > limit:
        logger.warning(
            f"🌐 [상품명] 대상 {len(todo)}개 중 이번 실행에서는 {limit}개만 처리 "
            f"(나머지는 다음 실행에서 이어서)"
        )
        todo = todo[:limit]

    logger.info(f"🌐 [상품명] 번역 대상 {len(todo)}개 / 캐시 스킵 {cached}개")

    # (pid, name) pairs for batch; keep hash map
    pairs = [(pid, name) for pid, name, _ in todo]
    hash_map = {pid: h for pid, _, h in todo}
    result_map = _run_batches(pairs, PROMPT_PRODUCT_NAME, "상품명")

    translated = 0
    for pid, en_name in result_map.items():
        conn.execute(
            "UPDATE products SET product_name_en=?, name_en_hash=? WHERE product_id=?",
            (en_name, hash_map[pid], pid),
        )
        translated += 1
    if translated:
        conn.commit()

    failed = len(todo) - translated
    logger.info(f"✅ [상품명] 완료: 신규/변경 {translated}개, 캐시 {cached}개, 실패 {failed}개")
    return {"translated": translated, "skipped_cached": cached, "failed": failed}


def _sync_unique_field(conn, src_col, en_col, hash_col, prompt_template, label, force_all=False):
    """
    고유값 단위로 번역 후, 같은 원문을 가진 모든 상품 행에 일괄 반영.
    브랜드·카테고리처럼 중복이 많은 필드에 사용.
    """
    rows = conn.execute(
        f"""
        SELECT DISTINCT {src_col} AS src,
               {en_col} AS en,
               {hash_col} AS h
        FROM products
        WHERE status='ACTIVE'
          AND {src_col} IS NOT NULL AND {src_col} != ''
        """
    ).fetchall()

    # 고유 원문별로 "번역이 필요한지" 판단
    # 같은 원문이라도 행마다 en/hash가 다를 수 있으므로,
    # 하나라도 캐시가 유효하면 그 번역을 재사용하고, 전부 없거나 force면 번역 대상에 넣음.
    from collections import defaultdict
    groups = defaultdict(list)  # src_text -> list of (en, h)
    for r in rows:
        groups[r["src"]].append((r["en"], r["h"]))

    todo_texts = []
    reuse = {}  # src -> already-good en
    cached = 0

    for src, variants in groups.items():
        h = _hash(src)
        good = None
        for en, stored_h in variants:
            if not force_all and en and stored_h == h:
                good = en
                break
        if good is not None:
            reuse[src] = good
            cached += 1
        else:
            todo_texts.append(src)

    logger.info(f"🌐 [{label}] 고유값 번역 대상 {len(todo_texts)}개 / 캐시 스킵 {cached}개")

    pairs = [(t, t) for t in todo_texts]  # key = 원문 자체
    result_map = _run_batches(pairs, prompt_template, label)

    # 새로 번역된 것 + 재사용할 것을 합쳐서 일괄 UPDATE
    update_map = dict(reuse)
    update_map.update(result_map)

    updated_rows = 0
    for src, en in update_map.items():
        h = _hash(src)
        cur = conn.execute(
            f"""
            UPDATE products
            SET {en_col}=?, {hash_col}=?
            WHERE {src_col}=? AND status='ACTIVE'
              AND (
                {en_col} IS NULL OR {en_col}='' OR {hash_col} IS NULL OR {hash_col} != ?
                OR ?
              )
            """,
            (en, h, src, h, 1 if force_all else 0),
        )
        updated_rows += cur.rowcount

    if updated_rows:
        conn.commit()

    translated = len(result_map)
    failed = len(todo_texts) - translated
    logger.info(
        f"✅ [{label}] 완료: 고유값 신규번역 {translated}개, 캐시 {cached}개, "
        f"실패 {failed}개, 상품행 갱신 {updated_rows}개"
    )
    return {"translated": translated, "skipped_cached": cached, "failed": failed, "rows_updated": updated_rows}


def sync_translations(conn, max_items=None, force_all=False):
    """
    신규/변경된 상품명·브랜드·카테고리를 Gemini로 번역 후 캐시(DB)에 저장한다.
    (일일 자동 실행 시 기본 동작 — 바뀐 것만 번역, 나머지는 캐시 재사용)

    force_all=True: 캐시를 무시하고 ACTIVE 상품 전체를 다시 번역.
      ⚠️ 수동 1회성 교정용. 매일 자동 실행에는 쓰지 말 것.
    """
    if not config.TRANSLATE_ENABLED:
        logger.info("🌐 번역 비활성화 상태(TRANSLATE_ENABLED=0) - 건너뜀")
        return {"product_name": {}, "brand": {}, "category": {}, "parent_category": {}}

    if not config.GEMINI_API_KEY:
        logger.warning("🌐 GEMINI_API_KEY 미설정 - 번역 단계 건너뜀 (기존 번역 캐시는 그대로 유지됨)")
        return {"product_name": {}, "brand": {}, "category": {}, "parent_category": {}}

    stats = {}

    # 1) 브랜드 (고유값 소수 → 먼저)
    stats["brand"] = _sync_unique_field(
        conn, "brand", "brand_en", "brand_en_hash",
        PROMPT_BRAND, "브랜드", force_all=force_all,
    )

    # 2) 카테고리
    stats["category"] = _sync_unique_field(
        conn, "category", "category_en", "category_en_hash",
        PROMPT_CATEGORY, "카테고리", force_all=force_all,
    )

    # 3) 상위 카테고리
    stats["parent_category"] = _sync_unique_field(
        conn, "parent_category", "parent_category_en", "parent_category_en_hash",
        PROMPT_CATEGORY, "상위카테고리", force_all=force_all,
    )

    # 4) 상품명 (건수가 많음)
    stats["product_name"] = _sync_product_names(conn, force_all=force_all, max_items=max_items)

    logger.info(f"✅ 전체 번역 동기화 완료: {stats}")
    return stats


if __name__ == "__main__":
    """
    수동 1회성 실행 진입점.

    사용 예:
        GEMINI_API_KEY=xxx python translate_service.py
        GEMINI_API_KEY=xxx python translate_service.py --force-all
        GEMINI_API_KEY=xxx python translate_service.py --force-all --max-items 500
    """
    import argparse
    import db as _db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="상품명/브랜드/카테고리 영어 번역 캐시 동기화")
    parser.add_argument("--force-all", action="store_true",
                         help="캐시를 무시하고 ACTIVE 전체를 다시 번역")
    parser.add_argument("--max-items", type=int, default=None,
                         help="상품명 번역 시 최대 개수 (브랜드/카테고리에는 적용 안 됨)")
    args = parser.parse_args()

    _conn = _db.connect()
    try:
        result = sync_translations(_conn, max_items=args.max_items, force_all=args.force_all)
        logger.info(f"=== 최종 결과: {result} ===")
    finally:
        _conn.close()

# -*- coding: utf-8 -*-
"""
Gemini API를 이용해 한글 텍스트를 영어로 번역하고 DB에 캐시로 저장한다.

두 가지 번역 대상을 다룬다:
  1) 상품명 (products.product_name -> product_name_en)
     - 상품마다 값이 달라서, "이름이 바뀐 상품"만 골라 번역한다.
     - products.name_en_hash 에 번역 당시 product_name의 MD5 해시를 저장해두고,
       다음 실행 때 현재 해시와 비교해서 같으면 API를 다시 부르지 않는다.
  2) 브랜드 / 카테고리 / 상위카테고리 (term_translations 테이블)
     - 수천 개 상품이 같은 브랜드/카테고리 값을 공유하므로, 상품별로 번역하면 낭비다.
     - 전체 상품에서 "고유값"만 뽑아서 term_translations(term_type, term_ko, term_en)에
       한 번만 저장해두고 캐시로 재사용한다. (브랜드 1,155개/카테고리 52개/상위카테고리 8개 수준
       이라 최초 1회만 비용이 들고, 그 뒤로는 새 브랜드가 나올 때만 소량 호출된다.)

두 작업 모두 같은 "요청 예산"(분당/일일 한도)을 공유해서, 합쳐서 한도를 넘기지 않는다.

필요 환경변수:
  GEMINI_API_KEY   (필수) - 없으면 번역 단계 자체를 건너뜀
  GEMINI_MODEL     (선택, 기본 config.GEMINI_MODEL)
  TRANSLATE_ENABLED / TRANSLATE_BATCH_SIZE / TRANSLATE_MAX_PER_RUN (선택)
  GEMINI_RPM_LIMIT / GEMINI_RPD_LIMIT / TRANSLATE_MAX_REQUESTS_PER_RUN (선택)
"""
import hashlib
import json
import logging
import time
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def _hash(name):
    return hashlib.md5((name or "").encode("utf-8")).hexdigest()


def _get_client():
    from google import genai  # 지연 import: GEMINI_API_KEY 없을 때 패키지 없어도 전체 파이프라인은 죽지 않게
    return genai.Client(api_key=config.GEMINI_API_KEY)


PRODUCT_PROMPT = """You will translate Korean cosmetics/beauty e-commerce product names into English.
Input is a JSON array of Korean product name strings, in order.

Rules:
- Output ONLY a JSON array of strings, same length and same order as the input. No explanation, no markdown fences.
- Keep brand names as their real romanized/English brand name when you recognize them (e.g. 메디힐 -> Mediheal, 토리든 -> Torriden).
- Translate ingredient names, product types, and marketing phrases into natural, fluent English used on real beauty e-commerce sites.
  Do NOT transliterate Korean words phonetically (e.g. never output things like "Nyeon" for 년 or "Beolseu" for 벌스) -
  always translate the *meaning*, not the sound. If a word's meaning is unclear, prefer a natural paraphrase over a phonetic guess.
- Keep numbers, units (ml, g, %), and bracket/parenthesis promo text, but translate the text inside them too.
- Keep it concise, like an actual English product listing title.

Input:
{items}
"""

BRAND_PROMPT = """You will convert Korean cosmetics brand names into their real, official English brand name.
Input is a JSON array of Korean brand name strings, in order.

Rules:
- Output ONLY a JSON array of strings, same length and same order as the input. No explanation, no markdown fences.
- Use the brand's actual official English name if you know it (e.g. 메디힐 -> Mediheal, 토리든 -> Torriden, 라운드랩 -> Round Lab).
- If you don't recognize the brand, give a natural Revised-Romanization-style transliteration instead of leaving it in Korean,
  but never invent a fake English brand name.

Input:
{items}
"""

CATEGORY_PROMPT = """You will translate Korean cosmetics/shopping category names into short, natural English category labels
used on e-commerce sites (e.g. 마스크팩 -> Mask Pack, 클렌징폼/젤 -> Cleansing Foam/Gel, 선크림 -> Sunscreen).
Input is a JSON array of Korean category strings, in order.

Rules:
- Output ONLY a JSON array of strings, same length and same order as the input. No explanation, no markdown fences.
- Keep it short (1-4 words), like a real category filter label.

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
        logger.error(f"Gemini 응답 JSON 파싱 실패: {e} / 응답 앞부분: {text[:300]!r}")
        return None
    if not isinstance(out, list) or len(out) != len(names):
        got = len(out) if isinstance(out, list) else type(out).__name__
        logger.error(f"Gemini 응답 개수 불일치 (요청 {len(names)} / 응답 {got})")
        return None
    return [str(x) for x in out]


class RequestBudget:
    """분당/일일 Gemini 호출 한도를 여러 번역 작업(상품명, 브랜드, 카테고리)이 공유해서 지킨다."""

    def __init__(self):
        self.max_requests = max(1, config.TRANSLATE_MAX_REQUESTS_PER_RUN)
        self.used = 0
        self.min_interval = (60.0 / config.GEMINI_RPM_LIMIT) * 1.2 if config.GEMINI_RPM_LIMIT > 0 else 0.5

    def remaining(self):
        return max(0, self.max_requests - self.used)

    def note_request(self):
        self.used += 1
        time.sleep(self.min_interval)


def _run_batches(todo, prompt_template, batch_size, budget, apply_fn, label):
    """todo: [(key, ko_text), ...]. apply_fn(batch, result_list)로 결과를 저장한다."""
    translated, failed = 0, 0
    total_batches_needed = -(-len(todo) // batch_size)  # ceil division

    if total_batches_needed > budget.remaining():
        allowed_items = budget.remaining() * batch_size
        logger.warning(
            f"🌐 [{label}] 필요한 호출 수({total_batches_needed})가 남은 예산({budget.remaining()})을 초과합니다. "
            f"{allowed_items}개만 처리하고 나머지는 다음 실행에서 이어서 처리합니다."
        )
        todo = todo[:allowed_items]

    for i in range(0, len(todo), batch_size):
        if budget.remaining() <= 0:
            logger.warning(f"🌐 [{label}] 요청 예산 소진 - 나머지는 다음 실행에서 처리")
            break

        batch = todo[i:i + batch_size]
        texts = [b[1] for b in batch]

        result = None
        for attempt in range(3):
            try:
                result = _translate_batch(texts, prompt_template)
                budget.note_request()
                if result:
                    break
            except Exception as e:
                budget.note_request()
                wait = 20 * (attempt + 1) if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) else 2 * (attempt + 1)
                logger.warning(f"🌐 [{label}] 배치 실패 (시도 {attempt + 1}/3): {e} - {wait}초 대기 후 재시도")
                time.sleep(wait)
                continue

        if not result:
            failed += len(batch)
            logger.error(f"🌐 [{label}] 배치 최종 실패, {len(batch)}개는 건너뜀 (다음 실행에서 자동 재시도)")
            continue

        apply_fn(batch, result)
        translated += len(batch)
        logger.info(f"🌐 [{label}] 진행: {min(i + batch_size, len(todo))}/{len(todo)}")

    return translated, failed


def sync_term_translations(conn, budget):
    """브랜드/카테고리/상위카테고리의 '고유값'만 골라 한 번씩만 번역해서 term_translations에 캐시."""
    results = {}
    specs = [
        ("brand", "brand", BRAND_PROMPT),
        ("category", "category", CATEGORY_PROMPT),
        ("parent_category", "parent_category", CATEGORY_PROMPT),
    ]

    for term_type, col, prompt in specs:
        all_values = [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM products WHERE {col} IS NOT NULL AND {col} != ''"
            ).fetchall()
        ]
        cached_rows = conn.execute(
            "SELECT term_ko FROM term_translations WHERE term_type=? AND term_en IS NOT NULL", (term_type,)
        ).fetchall()
        cached_set = {r[0] for r in cached_rows}

        todo = [(v, v) for v in all_values if v not in cached_set]
        if not todo:
            results[term_type] = {"translated": 0, "failed": 0, "cached": len(all_values)}
            continue

        def _apply(batch, result_list, term_type=term_type):
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for (ko, _), en in zip(batch, result_list):
                conn.execute(
                    "INSERT INTO term_translations (term_type, term_ko, term_en, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(term_type, term_ko) DO UPDATE SET term_en=excluded.term_en, updated_at=excluded.updated_at",
                    (term_type, ko, en, now),
                )
            conn.commit()

        t, f = _run_batches(todo, prompt, config.TRANSLATE_BATCH_SIZE, budget, _apply, label=f"용어:{term_type}")
        results[term_type] = {"translated": t, "failed": f, "cached": len(all_values) - len(todo)}
        logger.info(f"✅ [용어:{term_type}] 번역 {t}개 / 캐시 재사용 {results[term_type]['cached']}개 / 실패 {f}개")

    return results


def sync_translations(conn, budget, max_items=None):
    """신규/변경된 상품명만 골라 Gemini로 번역 후 캐시(DB)에 저장한다."""
    rows = conn.execute(
        "SELECT product_id, product_name, product_name_en, name_en_hash "
        "FROM products WHERE status='ACTIVE' AND product_name IS NOT NULL AND product_name != ''"
    ).fetchall()

    todo, cached = [], 0
    for r in rows:
        h = _hash(r["product_name"])
        if r["product_name_en"] and r["name_en_hash"] == h:
            cached += 1
            continue
        todo.append((r["product_id"], r["product_name"]))

    limit = max_items or config.TRANSLATE_MAX_PER_RUN
    if len(todo) > limit:
        logger.warning(
            f"🌐 [상품명] 번역 대상 {len(todo)}개 중 이번 실행에서는 {limit}개만 처리합니다 "
            f"(TRANSLATE_MAX_PER_RUN). 나머지는 다음 실행에서 이어서 처리됩니다."
        )
        todo = todo[:limit]

    logger.info(f"🌐 [상품명] 번역 대상 {len(todo)}개 / 캐시 재사용(스킵) {cached}개")

    def _apply(batch, result_list):
        for (pid, name), en_name in zip(batch, result_list):
            conn.execute(
                "UPDATE products SET product_name_en=?, name_en_hash=? WHERE product_id=?",
                (en_name, _hash(name), pid),
            )
        conn.commit()

    translated, failed = _run_batches(todo, PRODUCT_PROMPT, config.TRANSLATE_BATCH_SIZE, budget, _apply, label="상품명")

    logger.info(f"✅ [상품명] 번역 완료: 신규/변경 {translated}개, 캐시 재사용 {cached}개, 실패 {failed}개")
    return {"translated": translated, "skipped_cached": cached, "failed": failed}


def sync_all(conn):
    """용어(브랜드/카테고리) 먼저, 그다음 상품명 순으로 같은 예산 안에서 번역한다."""
    if not config.TRANSLATE_ENABLED:
        logger.info("🌐 번역 비활성화 상태(TRANSLATE_ENABLED=0) - 건너뜀")
        return {"terms": None, "products": None}

    if not config.GEMINI_API_KEY:
        logger.warning("🌐 GEMINI_API_KEY 미설정 - 번역 단계 건너뜀 (기존 번역 캐시는 그대로 유지됨)")
        return {"terms": None, "products": None}

    budget = RequestBudget()
    logger.info(f"🌐 번역 요청 예산: 이번 실행 최대 {budget.max_requests}회 (분당 {config.GEMINI_RPM_LIMIT}회 준수)")

    term_stats = sync_term_translations(conn, budget)
    product_stats = sync_translations(conn, budget)

    logger.info(f"🌐 이번 실행 총 사용 요청 수: {budget.used}/{budget.max_requests}")
    return {"terms": term_stats, "products": product_stats}

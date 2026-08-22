"""
Gemini API를 이용해 product_name을 영어로 번역하고 DB에 캐시로 저장한다.

캐시 동작 방식:
  - products.product_name_en : 번역 결과
  - products.name_en_hash    : 번역 당시 product_name의 MD5 해시
  - 매 실행 시 product_name의 현재 해시와 name_en_hash를 비교해서
    "이름이 바뀐 적 없는 상품"은 API를 다시 호출하지 않고 건너뛴다.
  - 즉 캐시는 별도 파일이 아니라 beauty_catalog.db 안에 그대로 저장되며,
    이 DB가 그대로 GitHub에 커밋되므로 다음 실행에서도 캐시가 유지된다.

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


PROMPT_TEMPLATE = """You will translate Korean cosmetics/beauty e-commerce product names into natural, fluent English,
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


def _translate_batch(names):
    client = _get_client()
    prompt = PROMPT_TEMPLATE.format(items=json.dumps(names, ensure_ascii=False))
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


def sync_translations(conn, max_items=None, force_all=False):
    """
    신규/변경된 상품명만 골라 Gemini로 번역 후 캐시(DB)에 저장한다.
    (일일 자동 실행 시 기본 동작 — 매일 새로 들어온 상품만 번역, 나머지는 캐시 재사용)

    force_all=True: 캐시를 무시하고 ACTIVE 상품 전체를 다시 번역한다.
      ⚠️ 매일 자동으로 돌리는 용도가 아니라, 과거에 저품질(로마자 표기 등)로 번역된
      기존 데이터를 한 번에 교정하고 싶을 때 수동으로만 사용한다.
      (python translate_service.py --force-all 로 CLI에서 실행)
    """
    if not config.TRANSLATE_ENABLED:
        logger.info("🌐 번역 비활성화 상태(TRANSLATE_ENABLED=0) - 건너뜀")
        return {"translated": 0, "skipped_cached": 0, "failed": 0}

    if not config.GEMINI_API_KEY:
        logger.warning("🌐 GEMINI_API_KEY 미설정 - 번역 단계 건너뜀 (기존 번역 캐시는 그대로 유지됨)")
        return {"translated": 0, "skipped_cached": 0, "failed": 0}

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

    # force_all(수동 1회성 전체 재번역)일 때는 기본적으로 todo 전체를 처리한다.
    # (TRANSLATE_MAX_PER_RUN은 "매일 자동 실행" 시 API 과다호출을 막기 위한 안전장치이므로
    #  수동으로 돌리는 force_all에는 적용하지 않는다. max_items로 명시적으로 제한 가능)
    limit = max_items if max_items is not None else (len(todo) if force_all else config.TRANSLATE_MAX_PER_RUN)
    if len(todo) > limit:
        logger.warning(
            f"🌐 번역 대상 {len(todo)}개 중 이번 실행에서는 {limit}개만 처리합니다 "
            f"(TRANSLATE_MAX_PER_RUN). 나머지는 다음 실행에서 이어서 처리됩니다."
        )
        todo = todo[:limit]

    logger.info(f"🌐 번역 대상 {len(todo)}개 / 캐시 재사용(스킵) {cached}개")

    translated, failed = 0, 0
    batch_size = max(1, config.TRANSLATE_BATCH_SIZE)

    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        names = [b[1] for b in batch]

        result = None
        for attempt in range(3):
            try:
                result = _translate_batch(names)
                if result:
                    break
            except Exception as e:
                logger.warning(f"🌐 번역 배치 실패 (시도 {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))

        if not result:
            failed += len(batch)
            logger.error(f"🌐 배치 번역 최종 실패, {len(batch)}개는 건너뜀 (다음 실행에서 자동 재시도)")
            continue

        for (pid, _, h), en_name in zip(batch, result):
            conn.execute(
                "UPDATE products SET product_name_en=?, name_en_hash=? WHERE product_id=?",
                (en_name, h, pid),
            )
            translated += 1

        conn.commit()
        logger.info(f"🌐 번역 진행: {min(i + batch_size, len(todo))}/{len(todo)}")
        time.sleep(0.5)  # 레이트리밋 여유

    logger.info(f"✅ 번역 완료: 신규/변경 {translated}개, 캐시 재사용 {cached}개, 실패 {failed}개")
    return {"translated": translated, "skipped_cached": cached, "failed": failed}


if __name__ == "__main__":
    """
    수동 1회성 실행 진입점.

    - 기존 12,000여개 상품명이 저품질(임의 로마자 표기 등)로 캐시되어 있는 것을
      개선된 프롬프트로 다시 번역하고 싶을 때 아래처럼 딱 한 번 실행한다.
    - GEMINI_API_KEY 환경변수가 필요하다 (이 컨테이너는 네트워크가 막혀있어
      이 스크립트를 실제로 실행할 수 없으므로, GitHub Actions 환경이나
      네트워크가 열린 로컬/서버 환경에서 실행해야 한다).

    사용 예:
        GEMINI_API_KEY=xxx TRANSLATE_BATCH_SIZE=40 python translate_service.py --force-all
        GEMINI_API_KEY=xxx python translate_service.py --force-all --max-items 500   # 테스트로 500개만
    """
    import argparse
    import db as _db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="상품명 영어 번역 캐시 동기화")
    parser.add_argument("--force-all", action="store_true",
                         help="캐시를 무시하고 ACTIVE 상품 전체를 다시 번역(기존 저품질 번역 교정용)")
    parser.add_argument("--max-items", type=int, default=None,
                         help="이번 실행에서 최대 몇 개까지 번역할지 (지정 안 하면 --force-all은 전체, 아니면 config 기본값)")
    args = parser.parse_args()

    _conn = _db.connect()
    try:
        result = sync_translations(_conn, max_items=args.max_items, force_all=args.force_all)
        logger.info(f"=== 최종 결과: {result} ===")
    finally:
        _conn.close()

"""
AI 감성 분석 모듈 (Claude API)

최적화 전략:
1. Gemini → Claude Haiku 4.5 전환
2. 배치 크기 10 → 50 (API 호출 70% 감소)
3. 로컬 키워드 사전 분류 강화 (50~60% 절감)
4. 제목 유사도 기반 중복 뉴스 제거 (30~40% 절감)
5. 1일 1회 배치 분석 (장 마감 후)
"""

import json
import logging
import re
import time
from datetime import datetime

import anthropic

from config import CLAUDE_API_KEY
from db import get_unanalyzed_news, update_news_sentiment
from notifier import _send as tg_send

logger = logging.getLogger(__name__)

BATCH_SIZE = 30  # 50→30: Claude 응답 ID 누락 방지
RETRY_COUNT = 2
BATCH_DELAY = 1  # Claude는 rate limit 여유로움

# 주요 상장사 목록 (로컬 분류 시 회사명 추출용)
MAJOR_COMPANIES = [
    "삼성전자", "SK하이닉스", "LG에너지솔루션", "삼성바이오로직스",
    "현대차", "기아", "셀트리온", "KB금융", "신한지주", "POSCO홀딩스",
    "NAVER", "네이버", "카카오", "삼성SDI", "LG화학", "현대모비스",
    "삼성물산", "삼성전기", "SK이노베이션", "SK텔레콤", "LG전자",
    "한국전력", "포스코퓨처엠", "에코프로비엠", "에코프로", "두산에너빌리티",
    "HD현대중공업", "한화에어로스페이스", "한미반도체", "크래프톤",
    "카카오뱅크", "카카오페이", "하이브", "엔씨소프트", "넷마블",
    "CJ제일제당", "아모레퍼시픽", "KT", "SKC", "한화솔루션",
    "두산밥캣", "고려아연", "LG이노텍", "SK바이오팜", "삼성생명",
    "한국가스공사", "대한항공", "HLB", "알테오젠", "리가켐바이오",
]

# ── 로컬 키워드 사전 (API 호출 절감) ──
# 주의: 뉴스 검색 키워드(급등, 상한가, 신고가, 대량매수)와 겹치는 단어는 제외
# (검색어가 제목/설명에 포함되어 모든 기사가 매칭되는 문제 방지)
POSITIVE_KEYWORDS = [
    "호재", "흑자전환", "실적개선", "매출증가", "영업이익증가",
    "수주", "계약체결", "특허취득", "FDA승인", "임상성공", "배당확대",
    "자사주매입", "목표가상향", "투자의견상향", "매수추천", "어닝서프라이즈",
    "사상최대실적", "턴어라운드", "수출호조", "대규모투자",
    "수혜주", "강세장", "반등성공",
]
NEGATIVE_KEYWORDS = [
    "급락", "하한가", "폭락", "악재", "적자", "적자전환", "실적부진",
    "매출감소", "영업손실", "순손실", "목표가하향", "투자의견하향",
    "공매도", "감자", "상장폐지", "횡령", "분식회계",
    "압수수색", "리콜", "부도", "파산", "워크아웃", "약세장",
    "반도체불황", "수출감소", "관세부과", "제재",
    "충돌", "전쟁", "긴장고조", "불안", "위기", "폭발", "봉쇄",
    "중단", "통제", "빚투", "하락우려", "인플레이션",
    "환율급등", "금리인상", "경기침체", "스태그플레이션",
]
NEUTRAL_KEYWORDS = [
    "보합", "관망", "횡보", "혼조세", "변동없", "유지",
]

# 주식 무관 기사 필터 (로컬 분류 시 스킵 → Claude에게 넘김)
NON_STOCK_INDICATORS = [
    "화재", "추락사", "사망", "살인", "살해", "스토킹", "성폭행",
    "교통사고", "캠페인", "선거", "예비후보", "경찰", "소방",
    "날씨", "기상", "지진", "태풍",
]

# 시스템 프롬프트
SYSTEM_PROMPT = """당신은 한국 주식 시장 전문 뉴스 분석가입니다.
주어진 뉴스 목록을 분석하여 반드시 JSON 배열만 반환하세요. 다른 텍스트는 포함하지 마세요."""

# 분석 요청 프롬프트 템플릿
ANALYSIS_PROMPT = """주식 투자자 관점에서 아래 뉴스들의 감성을 분석해주세요.
각 뉴스에 대해 sentiment(positive/negative/neutral), confidence(0.0~1.0), mentioned_companies(관련 상장사명), summary(투자 관점 한줄 요약)를 JSON 배열로 반환해주세요.

중요:
- 모든 뉴스에 대해 빠짐없이 결과를 반환하세요.
- id는 반드시 [ID:숫자] 에서 추출한 정수(integer)를 그대로 사용하세요.

반드시 아래 형식의 JSON 배열만 반환하세요:
[
  {{"id": 123, "sentiment": "positive|negative|neutral", "confidence": 0.8, "mentioned_companies": ["회사명1"], "summary": "한줄 요약"}}
]

뉴스 목록:
{news_list}"""


def _extract_companies(title: str, description: str = "") -> str:
    """제목/설명에서 주요 상장사명 추출"""
    text = f"{title} {description}"
    found = [c for c in MAJOR_COMPANIES if c in text]
    return ",".join(dict.fromkeys(found))  # 중복 제거, 순서 유지


def _classify_by_keywords(title: str, description: str = "") -> tuple[str, float] | None:
    """강화된 로컬 키워드 분류. 명확한 경우에만 반환."""
    text = f"{title} {description}"

    # 주식 무관 기사는 로컬 분류 스킵 → Claude에게 위임
    if any(ind in title for ind in NON_STOCK_INDICATORS):
        return None

    pos_score = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
    neg_score = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    neu_score = sum(1 for kw in NEUTRAL_KEYWORDS if kw in text)

    total = pos_score + neg_score + neu_score
    if total == 0:
        return None

    # 반드시 2개 이상 매칭되어야 로컬 분류 (단일 매칭은 오분류 위험)
    if pos_score >= 2 and pos_score > neg_score * 2:
        return ("positive", min(0.65 + pos_score * 0.05, 0.9))
    if neg_score >= 2 and neg_score > pos_score * 2:
        return ("negative", min(0.65 + neg_score * 0.05, 0.9))

    # 긍정+부정 혼재 시 Claude에게 위임
    if pos_score >= 1 and neg_score >= 1:
        return None

    # 중립은 단독 매칭 허용
    if neu_score >= 1 and pos_score == 0 and neg_score == 0:
        return ("neutral", 0.5)

    return None


def _deduplicate_news(news_rows: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """
    제목 유사도 기반 중복 제거.
    반환: (대표 뉴스 리스트, {중복 뉴스 id: 대표 뉴스 id} 매핑)
    """
    if not news_rows:
        return [], {}

    def _title_tokens(title: str) -> set:
        return set(re.findall(r'[가-힣a-zA-Z0-9]{2,}', title or ''))

    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    representatives = []  # (row, token_set)
    dup_map = {}  # dup_id -> representative_id

    for row in news_rows:
        tokens = _title_tokens(row["title"])
        is_dup = False
        for rep_row, rep_tokens in representatives:
            if _jaccard(tokens, rep_tokens) >= 0.6:
                dup_map[row["id"]] = rep_row["id"]
                is_dup = True
                break
        if not is_dup:
            representatives.append((row, tokens))

    return [r for r, _ in representatives], dup_map


def _build_news_text(news_rows: list[dict]) -> str:
    """뉴스 목록을 프롬프트용 텍스트로 변환"""
    lines = []
    for row in news_rows:
        title = row["title"] or ""
        desc = row["description"] or ""
        lines.append(f"[ID:{row['id']}] {title} / {desc}")
    return "\n".join(lines)


def _call_claude(news_rows: list[dict]) -> list[dict] | None:
    """Claude API 호출하여 감성 분석 결과 반환. 실패 시 None."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    news_text = _build_news_text(news_rows)
    user_prompt = ANALYSIS_PROMPT.format(news_list=news_text)

    for attempt in range(1 + RETRY_COUNT):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            text = response.content[0].text.strip()

            # JSON 추출
            codeblock_match = re.search(r"```(?:[Jj][Ss][Oo][Nn])?\s*\n(.*?)```", text, re.DOTALL)
            if codeblock_match:
                text = codeblock_match.group(1).strip()

            array_match = re.search(r"\[.*\]", text, re.DOTALL)
            if array_match:
                text = array_match.group(0)
            else:
                logger.error("Claude 응답에서 JSON 배열을 찾을 수 없음")
                if attempt < RETRY_COUNT:
                    time.sleep(1)
                    continue
                return None

            results = json.loads(text)
            if isinstance(results, list):
                logger.info("Claude API 성공: %d건 분석 (input=%d, output=%d tokens)",
                            len(results), response.usage.input_tokens, response.usage.output_tokens)
                return results

            logger.warning("Claude 응답이 리스트가 아님: %s", type(results))
            return None

        except json.JSONDecodeError as e:
            logger.warning("JSON 파싱 실패 (시도 %d/%d): %s", attempt + 1, 1 + RETRY_COUNT, e)
            if attempt < RETRY_COUNT:
                time.sleep(1)
            else:
                return None
        except anthropic.RateLimitError as e:
            logger.warning("Claude rate limit, 30초 대기 (시도 %d/%d)", attempt + 1, 1 + RETRY_COUNT)
            if attempt >= RETRY_COUNT:
                tg_send(f"⚠️ <b>Claude API Rate Limit</b>\n크레딧 소진 또는 요청 초과\n{e}")
            time.sleep(30)
        except anthropic.AuthenticationError as e:
            logger.error("Claude API 인증 실패: %s", e)
            tg_send(f"🚨 <b>Claude API 인증 실패</b>\nAPI 키를 확인하세요\n{e}")
            return None
        except Exception as e:
            if attempt < RETRY_COUNT:
                logger.warning("Claude API 호출 실패 (시도 %d/%d): %s", attempt + 1, 1 + RETRY_COUNT, e)
                time.sleep(2)
            else:
                logger.error("Claude API 호출 최종 실패: %s", e)
                tg_send(f"🚨 <b>Claude API 오류</b>\n감성분석 실패\n{e}")
                return None

    return None


def _learn_from_claude(news_rows: list[dict], result_map: dict):
    """Claude 분석 결과에서 키워드-감성 패턴을 추출하여 로컬 규칙 DB에 즉시 반영.
    다음 배치부터 동일 패턴은 로컬 분류 가능."""
    from db import get_connection
    keyword_sentiment = {}  # {keyword: {positive: n, negative: n, neutral: n}}

    for row in news_rows:
        r = result_map.get(row["id"])
        if not r or r.get("confidence", 0) < 0.7:
            continue
        sentiment = r.get("sentiment", "neutral")
        # 제목에서 2글자 이상 한글 단어 추출
        words = re.findall(r'[가-힣]{2,}', row["title"] or '')
        for word in words:
            if word in ("에서", "으로", "이번", "올해", "지난", "관련", "통해", "대한", "하는", "있는", "한다"):
                continue
            if sentiment not in ("positive", "negative", "neutral"):
                continue
            if word not in keyword_sentiment:
                keyword_sentiment[word] = {"positive": 0, "negative": 0, "neutral": 0}
            keyword_sentiment[word][sentiment] += 1

    if not keyword_sentiment:
        return

    conn = get_connection()
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        updated = 0
        for keyword, counts in keyword_sentiment.items():
            total = sum(counts.values())
            if total < 3:  # 최소 3건 이상
                continue
            dominant = max(counts, key=counts.get)
            ratio = counts[dominant] / total
            if ratio < 0.7:  # 70% 이상 한 감성에 집중
                continue
            conn.execute(
                """INSERT INTO local_sentiment_rules
                   (keyword, pattern, predicted_sentiment, confidence,
                    correct_count, total_count, accuracy, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(keyword, pattern) DO UPDATE SET
                       predicted_sentiment = excluded.predicted_sentiment,
                       confidence = excluded.confidence,
                       total_count = total_count + excluded.total_count,
                       accuracy = excluded.accuracy,
                       updated_at = excluded.updated_at""",
                (keyword, 'claude_feedback', dominant, round(ratio, 2),
                 0, total, round(ratio, 2), now)
            )
            updated += 1
        conn.commit()
        if updated:
            logger.info("로컬 규칙 학습: Claude 결과에서 %d개 키워드 패턴 반영", updated)
    except Exception:
        logger.exception("로컬 규칙 학습 실패")
    finally:
        conn.close()


def analyze_sentiment():
    """미분석 뉴스를 배치로 가져와 감성 분석 실행 (최적화 5단계 적용)"""
    if not CLAUDE_API_KEY:
        logger.warning("CLAUDE_API_KEY가 설정되지 않음. 감성 분석 건너뜀.")
        return

    total_analyzed = 0
    local_classified = 0
    dedup_count = 0
    claude_calls = 0
    batch_num = 0

    # DB 학습 기반 로컬 분류기
    try:
        from strategy.learner import classify_locally as db_classify
    except ImportError:
        db_classify = None

    while True:
        news_rows = get_unanalyzed_news(limit=BATCH_SIZE)
        if not news_rows:
            break

        # ── 1단계: 로컬 키워드 분류 ──
        claude_needed = []
        for row in news_rows:
            # DB 학습 규칙 먼저 시도
            result = None
            if db_classify:
                result = db_classify(row["title"], row.get("description", ""))
            # 키워드 사전 분류
            if not result:
                result = _classify_by_keywords(row["title"], row.get("description", ""))

            if result:
                sentiment, confidence = result
                companies = _extract_companies(row["title"], row.get("description", ""))
                update_news_sentiment(
                    news_id=row["id"],
                    sentiment=sentiment,
                    confidence=confidence,
                    companies=companies,
                    summary="[로컬 분류]",
                )
                local_classified += 1
                total_analyzed += 1
            else:
                claude_needed.append(row)

        if not claude_needed:
            continue

        # ── 2단계: 중복 제거 ──
        unique_news, dup_map = _deduplicate_news(claude_needed)
        dedup_count += len(dup_map)

        if not unique_news:
            continue

        batch_num += 1
        logger.info("배치 #%d: 원본 %d건 → 로컬 %d건 + 중복 %d건 제거 → Claude %d건",
                     batch_num, len(news_rows), local_classified, len(dup_map), len(unique_news))

        # ── 3단계: Claude API 배치 호출 ──
        results = _call_claude(unique_news)
        claude_calls += 1

        if results is None:
            logger.warning("배치 #%d Claude 분석 실패", batch_num)
            for row in unique_news:
                update_news_sentiment(
                    news_id=row["id"],
                    sentiment="error",
                    confidence=0.0,
                    companies="",
                    summary="분석 실패",
                )
            # 중복 뉴스도 error 처리
            for dup_id in dup_map:
                update_news_sentiment(
                    news_id=dup_id,
                    sentiment="error",
                    confidence=0.0,
                    companies="",
                    summary="분석 실패 (중복)",
                )
            continue

        # ── 4단계: 결과 저장 + 중복 뉴스에 결과 복제 ──
        result_map = {}
        for r in results:
            rid = r.get("id")
            if rid is not None:
                try:
                    result_map[int(rid)] = r
                except (ValueError, TypeError):
                    result_map[rid] = r

        # 폴백: ID 매칭 안 된 뉴스를 제목 유사도로 매칭
        unmatched_news = [row for row in unique_news if row["id"] not in result_map]
        if unmatched_news:
            orphan_results = [r for r in results if r not in result_map.values()]
            if orphan_results:
                for row in unmatched_news:
                    best_match = None
                    best_score = 0
                    row_tokens = set(re.findall(r'[가-힣a-zA-Z0-9]{2,}', row["title"] or ''))
                    for r in orphan_results:
                        summary = r.get("summary", "")
                        r_tokens = set(re.findall(r'[가-힣a-zA-Z0-9]{2,}', summary))
                        if not row_tokens or not r_tokens:
                            continue
                        score = len(row_tokens & r_tokens) / len(row_tokens | r_tokens)
                        if score > best_score and score > 0.3:
                            best_score = score
                            best_match = r
                    if best_match:
                        result_map[row["id"]] = best_match
                        orphan_results.remove(best_match)
                        logger.debug("폴백 매칭: news_id=%d (score=%.2f)", row["id"], best_score)
            if unmatched_news:
                still_unmatched = len([r for r in unique_news if r["id"] not in result_map])
                if still_unmatched:
                    logger.warning("Claude 응답 ID 누락: %d건 매칭 실패", still_unmatched)

        for row in unique_news:
            r = result_map.get(row["id"])
            if r:
                companies = ",".join(r.get("mentioned_companies", []))
                update_news_sentiment(
                    news_id=row["id"],
                    sentiment=r.get("sentiment", "neutral"),
                    confidence=r.get("confidence", 0.0),
                    companies=companies,
                    summary=r.get("summary", ""),
                )
                total_analyzed += 1
            else:
                update_news_sentiment(
                    news_id=row["id"],
                    sentiment="unknown",
                    confidence=0.0,
                    companies="",
                    summary="응답 누락",
                )

        # ── 5단계: Claude 결과를 로컬 규칙에 피드백 ──
        _learn_from_claude(unique_news, result_map)

        # 중복 뉴스에 대표 뉴스의 결과 복제
        for dup_id, rep_id in dup_map.items():
            r = result_map.get(rep_id)
            if r:
                companies = ",".join(r.get("mentioned_companies", []))
                update_news_sentiment(
                    news_id=dup_id,
                    sentiment=r.get("sentiment", "neutral"),
                    confidence=r.get("confidence", 0.0),
                    companies=companies,
                    summary=f"[중복] {r.get('summary', '')}",
                )
                total_analyzed += 1
            else:
                update_news_sentiment(
                    news_id=dup_id,
                    sentiment="unknown",
                    confidence=0.0,
                    companies="",
                    summary="중복 원본 응답 누락",
                )

        if len(unique_news) == BATCH_SIZE:
            time.sleep(BATCH_DELAY)

    logger.info("감성 분석 완료: 총 %d건 (로컬: %d건, 중복제거: %d건, Claude API: %d회)",
                total_analyzed, local_classified, dedup_count, claude_calls)

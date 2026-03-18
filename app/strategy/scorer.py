"""
종목 스코어링 모듈

뉴스 감성분석 + DART 이벤트 + 유동성 필터 + 언급 가속도를 종합하여
종목별 투자 스코어를 산출한다.

base_score = normalize(언급빈도)*W1 + 긍정비율*W2 + 평균신뢰도*W3
+ DART 호재 가점 (0 ~ +0.06)
+ 언급 가속도 가점 (mention>=5일 때, 0 ~ +0.03)
"""

import logging
import json
from collections import defaultdict

# pykrx 내부 로깅 버그 방지 (logging.info(args, kwargs) 포맷 에러)
logging.getLogger("pykrx").setLevel(logging.WARNING)

from config import (
    WEIGHT_MENTION_FREQ, WEIGHT_POSITIVE_RATIO,
    WEIGHT_CONFIDENCE, SCORE_LOOKBACK_DAYS,
)
from db import get_analyzed_news_by_company, get_stock_code_map, refresh_stock_codes

logger = logging.getLogger(__name__)

# 종목명→종목코드 캐시 (프로세스 수명 동안 유지)
_ticker_cache: dict[str, str] = {}
_ticker_cache_loaded: bool = False


def _get_learned_weights():
    """학습된 가중치 조회 (dynamic_config → learner → config 기본값)"""
    # 1순위: 반성 엔진의 동적 파라미터
    try:
        from config import get_param
        from strategy.reflection import _get_all_dynamic_config
        dc = _get_all_dynamic_config()
        if any(k in dc for k in ["WEIGHT_MENTION_FREQ", "WEIGHT_POSITIVE_RATIO", "WEIGHT_CONFIDENCE"]):
            return (
                dc.get("WEIGHT_MENTION_FREQ", get_param("WEIGHT_MENTION_FREQ")),
                dc.get("WEIGHT_POSITIVE_RATIO", get_param("WEIGHT_POSITIVE_RATIO")),
                dc.get("WEIGHT_CONFIDENCE", get_param("WEIGHT_CONFIDENCE")),
            )
    except Exception:
        pass

    # 2순위: learner의 가중치 이력
    try:
        from strategy.learner import get_current_weights
        w = get_current_weights()
        return w["w_mention_freq"], w["w_positive_ratio"], w["w_confidence"]
    except Exception:
        return WEIGHT_MENTION_FREQ, WEIGHT_POSITIVE_RATIO, WEIGHT_CONFIDENCE


def _load_ticker_cache():
    """DB stock_codes 테이블에서 종목명→종목코드 매핑 로드"""
    global _ticker_cache, _ticker_cache_loaded
    if _ticker_cache_loaded:
        return
    _ticker_cache_loaded = True
    try:
        _ticker_cache.update(get_stock_code_map())
        if not _ticker_cache:
            logger.info("종목 캐시 비어있음 → KRX에서 다운로드")
            count = refresh_stock_codes()
            if count > 0:
                _ticker_cache.update(get_stock_code_map())
        logger.info("종목 캐시 로드 완료: %d개", len(_ticker_cache))
    except Exception:
        logger.exception("종목 캐시 로드 실패")


def reload_ticker_cache():
    """종목 캐시 강제 리로드 (스케줄러에서 호출)"""
    global _ticker_cache, _ticker_cache_loaded
    _ticker_cache.clear()
    _ticker_cache_loaded = False
    count = refresh_stock_codes()
    _ticker_cache.update(get_stock_code_map())
    logger.info("종목 캐시 리로드 완료: %d개", len(_ticker_cache))
    return count


def find_stock_code(company_name: str) -> str | None:
    """종목명으로 종목코드 검색 (부분 매칭 지원)"""
    _load_ticker_cache()

    # 정확히 일치
    if company_name in _ticker_cache:
        return _ticker_cache[company_name]

    # 부분 매칭 (예: "삼성전자" in "삼성전자우")
    for name, code in _ticker_cache.items():
        if company_name in name or name in company_name:
            return code

    return None


# ── DART 이벤트 분류 룰 ──
# 강한 악재: 매수 차단 (score 계산 이전에 continue)
DART_BLOCK_KEYWORDS = [
    "유상증자결정", "유상증자", "감자결정", "감자",
    "상장폐지", "관리종목지정",
    "횡령", "배임", "분식회계",
    "감사의견거절", "감사의견한정",
    "워크아웃", "회생절차",
]

# 호재 가점 (3단계: +0.02, +0.04, +0.06)
DART_BONUS_RULES = {
    # +0.06: 강한 호재
    "자기주식취득": 0.06, "자사주매입": 0.06, "자사주취득": 0.06,
    "흑자전환": 0.06,
    # +0.04: 중간 호재
    "대규모공급계약": 0.04, "수주": 0.04, "계약체결": 0.04,
    "영업이익증가": 0.04, "실적개선": 0.04,
    "특허취득": 0.04, "FDA승인": 0.04, "임상성공": 0.04,
    # +0.02: 약한 호재
    "배당확대": 0.02, "배당결정": 0.02,
    "무상증자": 0.02,
}


def _get_dart_events(days: int = 3) -> dict[str, dict]:
    """최근 N일 DART 공시에서 종목별 이벤트 분류.
    Returns: {corp_name: {"blocked": bool, "bonus": float, "events": [str]}}
    """
    from db import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT corp_name, report_nm FROM dart_disclosure
               WHERE rcept_dt >= date('now', 'localtime', ?)""",
            (f"-{days} days",),
        ).fetchall()
    finally:
        conn.close()

    results = {}
    for corp_name, report_nm in rows:
        if not corp_name or not report_nm:
            continue

        if corp_name not in results:
            results[corp_name] = {"blocked": False, "bonus": 0.0, "events": []}

        # 차단 체크
        for kw in DART_BLOCK_KEYWORDS:
            if kw in report_nm:
                results[corp_name]["blocked"] = True
                results[corp_name]["events"].append(f"[차단]{kw}")
                break

        # 호재 가점
        if not results[corp_name]["blocked"]:
            for kw, bonus in DART_BONUS_RULES.items():
                if kw in report_nm:
                    results[corp_name]["bonus"] = max(results[corp_name]["bonus"], bonus)
                    results[corp_name]["events"].append(f"[+{bonus}]{kw}")
                    break  # 공시당 최대 1개 매칭

    return results


def _get_liquidity(stock_code: str) -> dict | None:
    """pykrx로 유동성 지표 조회. 5일 평균 거래대금 + 시가총액."""
    try:
        from pykrx import stock as pykrx_stock
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df = pykrx_stock.get_market_ohlcv_by_date(start, today, stock_code)
        if df.empty:
            return None
        recent = df.tail(5)
        avg_trade_value = float(recent["거래대금"].mean()) if "거래대금" in recent.columns else 0
        # 시가총액
        market_cap = None
        try:
            cap_df = pykrx_stock.get_market_cap_by_date(start, today, stock_code)
            if not cap_df.empty:
                market_cap = float(cap_df.iloc[-1]["시가총액"])
        except Exception:
            pass
        return {"avg_trade_value": avg_trade_value, "market_cap": market_cap}
    except Exception:
        return None


def _get_mention_acceleration(company_name: str, lookback_days: int) -> float:
    """언급 가속도: 최근 1일 언급 / 전체 기간 일평균. 1.0=평균, >1=가속."""
    from db import get_connection
    conn = get_connection()
    try:
        recent = conn.execute(
            """SELECT COUNT(*) FROM news
               WHERE mentioned_companies LIKE ?
                 AND analyzed_at >= datetime('now', 'localtime', '-1 day')""",
            (f"%{company_name}%",)
        ).fetchone()[0]
        total = conn.execute(
            """SELECT COUNT(*) FROM news
               WHERE mentioned_companies LIKE ?
                 AND analyzed_at >= datetime('now', 'localtime', ?)""",
            (f"%{company_name}%", f"-{lookback_days} days")
        ).fetchone()[0]
        daily_avg = total / lookback_days if lookback_days > 0 else 0
        if daily_avg > 0:
            return round(recent / daily_avg, 2)
        return 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


def get_company_scores(lookback_days: int = None) -> list[dict]:
    """
    종목별 투자 스코어 산출

    Returns:
        [{stock_code, stock_name, score, mention_count,
          positive_count, negative_count, avg_confidence,
          dart_events, mention_accel, avg_trade_value, market_cap}, ...]
    """
    if lookback_days is None:
        lookback_days = SCORE_LOOKBACK_DAYS

    _load_ticker_cache()

    # 최근 N일 감성분석 뉴스 조회
    news_list = get_analyzed_news_by_company(days=lookback_days)
    if not news_list:
        logger.info("스코어링할 뉴스 없음 (최근 %d일)", lookback_days)
        return []

    # 종목별 집계
    company_stats = defaultdict(lambda: {
        "mention_count": 0,
        "positive_count": 0,
        "negative_count": 0,
        "confidence_sum": 0.0,
    })

    for news in news_list:
        companies_str = news.get("mentioned_companies", "")
        if not companies_str:
            continue

        # mentioned_companies: JSON 배열 또는 콤마 구분 문자열
        try:
            companies = json.loads(companies_str)
        except (json.JSONDecodeError, TypeError):
            companies = [c.strip() for c in companies_str.split(",") if c.strip()]

        sentiment = news.get("sentiment", "neutral")
        confidence = news.get("confidence", 0.5) or 0.5

        for company in companies:
            stats = company_stats[company]
            stats["mention_count"] += 1
            stats["confidence_sum"] += confidence
            if sentiment == "positive":
                stats["positive_count"] += 1
            elif sentiment == "negative":
                stats["negative_count"] += 1

    if not company_stats:
        return []

    # 최대 언급 수 (정규화용)
    max_mentions = max(s["mention_count"] for s in company_stats.values())
    if max_mentions == 0:
        max_mentions = 1

    # DART 이벤트 분류
    dart_events = _get_dart_events(days=lookback_days)

    # 가중치 (루프 밖에서 1회만 조회)
    w_mf, w_pr, w_cf = _get_learned_weights()
    w_total = w_mf + w_pr + w_cf
    if w_total == 0:
        w_total = 1.0

    # 유동성 캐시 (pykrx 호출 최소화)
    liquidity_cache: dict[str, dict | None] = {}

    # 스코어 계산
    results = []
    for company_name, stats in company_stats.items():
        stock_code = find_stock_code(company_name)
        if not stock_code:
            logger.debug("종목코드 미발견: %s", company_name)
            continue

        mention_count = stats["mention_count"]

        # 최소 언급 필터 (노이즈 제거)
        if mention_count < 3:
            continue

        # DART 악재 차단
        dart_info = dart_events.get(company_name, {"blocked": False, "bonus": 0.0, "events": []})
        if dart_info["blocked"]:
            logger.debug("DART 차단: %s 악재=%s", company_name, dart_info["events"])
            continue

        # 유동성 필터 (5일 평균 거래대금 3억 미만 제외)
        if stock_code not in liquidity_cache:
            liquidity_cache[stock_code] = _get_liquidity(stock_code)
        liq = liquidity_cache[stock_code]
        if liq is not None and liq["avg_trade_value"] < 300_000_000:
            logger.debug("유동성 필터: %s (%s) 거래대금 %.1f억 < 3억",
                         company_name, stock_code, liq["avg_trade_value"] / 1e8)
            continue

        positive_count = stats["positive_count"]
        negative_count = stats["negative_count"]
        total_sentiment = positive_count + negative_count

        # 정규화된 언급빈도 (0~1)
        norm_mention = mention_count / max_mentions

        # 긍정 비율 (0~1)
        positive_ratio = positive_count / total_sentiment if total_sentiment > 0 else 0.5

        # 평균 신뢰도 (0~1)
        avg_confidence = stats["confidence_sum"] / mention_count if mention_count > 0 else 0.5

        # base_score (뉴스 감성 기반)
        base_score = (
            norm_mention * (w_mf / w_total)
            + positive_ratio * (w_pr / w_total)
            + avg_confidence * (w_cf / w_total)
        )

        # DART 호재 가점 (0 ~ +0.06)
        dart_bonus = dart_info["bonus"]

        # 언급 가속도 가점 (mention>=5일 때만, 0 ~ +0.03)
        accel = 0.0
        accel_bonus = 0.0
        if mention_count >= 5:
            accel = _get_mention_acceleration(company_name, lookback_days)
            accel_bonus = min(accel / 3.0, 1.0) * 0.03  # 3배 가속 = +0.03 cap

        # 최종 스코어
        score = base_score + dart_bonus + accel_bonus

        results.append({
            "stock_code": stock_code,
            "stock_name": company_name,
            "score": round(score, 4),
            "mention_count": mention_count,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "avg_confidence": round(avg_confidence, 4),
            "dart_bonus": dart_bonus,
            "dart_events": dart_info["events"],
            "mention_accel": accel,
            "avg_trade_value": liq["avg_trade_value"] if liq else None,
            "market_cap": liq["market_cap"] if liq else None,
        })

    # 스코어 내림차순 정렬
    results.sort(key=lambda x: x["score"], reverse=True)
    logger.info("종목 스코어 산출 완료: %d개 (DART 이벤트: %d종목, 유동성 필터 적용)",
                len(results), len(dart_events))
    return results

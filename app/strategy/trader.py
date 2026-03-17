"""
매매 실행 모듈

pykrx로 현재가를 조회하고 가상 매매를 기록한다.
(실제 증권사 API 연동이 아닌 시뮬레이션)
"""

import json
import logging
from datetime import datetime

from pykrx import stock as pykrx_stock

from config import MAX_PER_STOCK
from db import upsert_holding, delete_holding, insert_trade

logger = logging.getLogger(__name__)


def get_current_price(stock_code: str) -> float | None:
    """pykrx로 최근 거래일 종가 조회"""
    try:
        today = datetime.now().strftime("%Y%m%d")
        # 최근 5거래일 조회 → 마지막 행 종가
        df = pykrx_stock.get_market_ohlcv_by_date(
            fromdate=today, todate=today, ticker=stock_code
        )
        if df.empty:
            # 오늘 데이터 없으면 최근 5일 조회
            from datetime import timedelta
            start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
            df = pykrx_stock.get_market_ohlcv_by_date(
                fromdate=start, todate=today, ticker=stock_code
            )
        if not df.empty:
            return float(df.iloc[-1]["종가"])
        logger.warning("가격 조회 실패 (데이터 없음): %s", stock_code)
        return None
    except Exception:
        logger.exception("가격 조회 실패: %s", stock_code)
        return None


def get_realtime_price(stock_code: str) -> float | None:
    """네이버 금융에서 실시간 현재가 조회 (장중용)"""
    try:
        import requests
        from bs4 import BeautifulSoup
        url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        price_tag = soup.select_one("p.no_today span.blind")
        if price_tag:
            return float(price_tag.text.replace(",", ""))
        logger.warning("실시간 가격 파싱 실패: %s", stock_code)
        return None
    except Exception:
        logger.exception("실시간 가격 조회 실패: %s", stock_code)
        return None


def estimate_buy_price(stock_code: str) -> float | None:
    """매수 체결가 추정. 종가 + 유동성 기반 슬리피지. (실제 시가가 아닌 추정값)"""
    try:
        from datetime import timedelta
        today = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        df = pykrx_stock.get_market_ohlcv_by_date(
            fromdate=start, todate=today, ticker=stock_code
        )
        if df.empty:
            return None
        last_row = df.iloc[-1]
        close = float(last_row["종가"])
        open_price = float(last_row["시가"])
        volume = float(last_row["거래량"])

        # 유동성 기반 슬리피지: 거래량 적으면 슬리피지 크게
        if volume > 0:
            avg_amount = float(last_row.get("거래대금", 0))
            if avg_amount < 1_000_000_000:  # 거래대금 10억 미만
                slippage = 0.01  # 1%
            elif avg_amount < 5_000_000_000:  # 50억 미만
                slippage = 0.005  # 0.5%
            else:
                slippage = 0.002  # 0.2%
        else:
            slippage = 0.01

        # 장 마감 후 매수 → 종가 + 슬리피지 (다음날 시가 근사)
        estimated_price = round(close * (1 + slippage))
        logger.debug("체결가 추정: %s 종가=%d 슬리피지=%.1f%% → %d",
                     stock_code, close, slippage * 100, estimated_price)
        return estimated_price
    except Exception:
        logger.exception("시가 추정 실패: %s", stock_code)
        return None


def estimate_sell_price(stock_code: str) -> float | None:
    """매도 체결가 추정. 종가 - 유동성 기반 슬리피지. (실제 체결가가 아닌 추정값)"""
    try:
        from datetime import timedelta
        today = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        df = pykrx_stock.get_market_ohlcv_by_date(
            fromdate=start, todate=today, ticker=stock_code
        )
        if df.empty:
            return None
        last_row = df.iloc[-1]
        close = float(last_row["종가"])
        avg_amount = float(last_row.get("거래대금", 0))

        if avg_amount < 1_000_000_000:
            slippage = 0.008  # 0.8%
        elif avg_amount < 5_000_000_000:
            slippage = 0.003  # 0.3%
        else:
            slippage = 0.001  # 0.1%

        return round(close * (1 - slippage))
    except Exception:
        logger.exception("매도가 추정 실패: %s", stock_code)
        return None


def execute_buy(stock_code: str, stock_name: str, score: float,
                cash: float) -> dict | None:
    """
    매수 실행

    Args:
        stock_code: 종목코드
        stock_name: 종목명
        score: 투자 스코어
        cash: 가용 현금

    Returns:
        매수 결과 dict 또는 None (매수 불가)
    """
    price = estimate_buy_price(stock_code)
    if price is None or price <= 0:
        price = get_current_price(stock_code)
    if price is None or price <= 0:
        logger.warning("매수 불가 (가격 조회 실패): %s", stock_code)
        return None

    # 투자 금액: 종목당 최대 금액 vs 가용 현금 중 작은 값
    invest_amount = min(MAX_PER_STOCK, cash)
    quantity = int(invest_amount // price)

    if quantity <= 0:
        logger.info("매수 불가 (수량 0): %s, 가격=%d, 가용현금=%.0f", stock_code, price, cash)
        return None

    total_amount = quantity * price
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # DB에 보유 종목 기록
    upsert_holding(
        stock_code=stock_code,
        stock_name=stock_name,
        quantity=quantity,
        avg_price=price,
        current_price=price,
        highest_price=price,
        bought_at=now,
    )

    # 매매 기록
    insert_trade(
        stock_code=stock_code,
        stock_name=stock_name,
        trade_type="BUY",
        quantity=quantity,
        price=price,
        total_amount=total_amount,
        reason=f"스코어 {score:.2f} 매수 신호",
        score=score,
    )

    result = {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "quantity": quantity,
        "price": price,
        "total_amount": total_amount,
        "score": score,
    }
    logger.info("매수 완료: %s %s %d주 @%d (총 %.0f원)",
                stock_code, stock_name, quantity, price, total_amount)

    # 예측 기록 (학습 시스템용) — 스코어 기여 뉴스만 정확 매칭
    try:
        from strategy.learner import record_prediction
        from db import get_connection
        from config import SCORE_LOOKBACK_DAYS
        from collections import Counter
        conn = get_connection()
        conn.row_factory = __import__('sqlite3').Row
        # 스코어 참조 기간 내 + 감성분석 완료 뉴스
        candidate_rows = conn.execute(
            """SELECT id, sentiment, confidence, mentioned_companies FROM news
               WHERE sentiment IN ('positive', 'negative', 'neutral')
                 AND analyzed_at >= datetime('now', 'localtime', ?)
                 AND mentioned_companies IS NOT NULL
               ORDER BY confidence DESC""",
            (f"-{SCORE_LOOKBACK_DAYS} days",)
        ).fetchall()
        conn.close()
        # 정확 매칭: mentioned_companies에서 종목명 포함 여부 확인
        matched = []
        for r in candidate_rows:
            mc = r['mentioned_companies'] or ''
            try:
                companies = json.loads(mc)
            except (json.JSONDecodeError, TypeError):
                companies = [c.strip() for c in mc.split(",") if c.strip()]
            if stock_name in companies:
                matched.append(r)
            if len(matched) >= 10:
                break
        if matched:
            news_ids = [r['id'] for r in matched]
            sentiments = [r['sentiment'] for r in matched]
            avg_sentiment = Counter(sentiments).most_common(1)[0][0]
            avg_confidence = sum(r['confidence'] or 0.5 for r in matched) / len(matched)
            record_prediction(
                stock_code=stock_code,
                stock_name=stock_name,
                news_ids=news_ids,
                sentiment=avg_sentiment,
                confidence=avg_confidence,
                score=score,
                price=price,
            )
            logger.info("예측 기록: %s %d건 뉴스 연결 (lookback=%d일, 정확매칭)",
                        stock_code, len(news_ids), SCORE_LOOKBACK_DAYS)
    except Exception:
        logger.debug("예측 기록 실패 (비치명적)")

    # 텔레그램 알림
    try:
        from notifier import notify_buy
        notify_buy(stock_name, stock_code, quantity, price, total_amount, score)
    except Exception:
        logger.debug("매수 알림 전송 실패")

    return result


def execute_sell(stock_code: str, reason: str, holding_info: dict) -> dict:
    """
    매도 실행

    Args:
        stock_code: 종목코드
        reason: 매도 사유
        holding_info: 보유 종목 정보 dict

    Returns:
        매도 결과 dict
    """
    price = estimate_sell_price(stock_code)
    if price is None:
        price = get_current_price(stock_code)
    if price is None:
        # 현재가 조회 실패 시 기존 현재가 사용
        price = holding_info.get("current_price", holding_info["avg_price"])

    from config import BUY_FEE_RATE, SELL_FEE_RATE, SELL_TAX_RATE

    quantity = holding_info["quantity"]
    avg_price = holding_info["avg_price"]
    stock_name = holding_info["stock_name"]
    total_amount = quantity * price

    # 손익 계산 (수수료+세금 반영)
    buy_fee_per_share = avg_price * BUY_FEE_RATE
    sell_fee_per_share = price * (SELL_FEE_RATE + SELL_TAX_RATE)
    pnl = (price - avg_price - buy_fee_per_share - sell_fee_per_share) * quantity
    pnl = round(pnl)

    # 매매 기록
    insert_trade(
        stock_code=stock_code,
        stock_name=stock_name,
        trade_type="SELL",
        quantity=quantity,
        price=price,
        total_amount=total_amount,
        reason=reason,
        pnl=pnl,
    )

    # 보유 종목에서 삭제
    delete_holding(stock_code)

    result = {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "quantity": quantity,
        "price": price,
        "total_amount": total_amount,
        "pnl": pnl,
        "reason": reason,
    }
    logger.info("매도 완료: %s %s %d주 @%d (손익: %+.0f원) [%s]",
                stock_code, stock_name, quantity, price, pnl, reason)

    # 텔레그램 알림
    try:
        from notifier import notify_sell
        notify_sell(stock_name, stock_code, quantity, price, pnl, reason)
    except Exception:
        logger.debug("매도 알림 전송 실패")

    return result

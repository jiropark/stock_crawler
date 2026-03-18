"""
포트폴리오 관리 모듈

리밸런싱 로직:
1. 보유 종목 현재가 갱신
2. 손절 체크 (-5%)
3. 트레일링 스탑 (-3% from highest)
4. highest_price 갱신
5. 빈 슬롯 있으면 스코어 상위 종목 매수
6. 일별 성과 기록
"""

import logging
from datetime import datetime

from config import (
    INITIAL_CAPITAL, MAX_HOLDINGS, STOP_LOSS_PCT,
    TRAILING_STOP_PCT, SCORE_THRESHOLD, get_param,
)
from db import (
    get_holdings, update_holding_price, get_latest_cash, get_total_fees,
    insert_daily_performance, get_daily_performances, get_win_loss_stats,
)
from strategy.scorer import get_company_scores
from strategy.trader import get_current_price, get_realtime_price, execute_buy, execute_sell

logger = logging.getLogger(__name__)


def _get_cooldown_codes(days: int = 7) -> set:
    """최근 N일 내 손절/트레일링 스탑 매도한 종목코드 (재매수 금지)"""
    from db import get_connection
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT DISTINCT stock_code FROM trades
               WHERE trade_type = 'SELL'
                 AND (reason LIKE '%손절%' OR reason LIKE '%트레일링%')
                 AND traded_at >= datetime('now', 'localtime', ?)""",
            (f"-{days} days",)
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        logger.exception("쿨다운 종목 조회 실패")
        return set()


def _get_today_buy_count() -> int:
    """오늘 매수한 종목 수"""
    from db import get_connection
    try:
        conn = get_connection()
        row = conn.execute(
            """SELECT COUNT(*) FROM trades
               WHERE trade_type = 'BUY'
                 AND date(traded_at) = date('now', 'localtime')"""
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def monitor_positions():
    """장중 포지션 모니터링 — 보유 종목 현재가 체크 후 손절/트레일링 스탑 실행"""
    from strategy.trader import get_realtime_price

    holdings = get_holdings()
    if not holdings:
        return

    logger.info("포지션 모니터링: %d개 종목 체크", len(holdings))

    cash = get_latest_cash()
    sold = False

    for h in holdings:
        code = h["stock_code"]
        price = get_realtime_price(code)
        if price is None:
            logger.warning("현재가 조회 실패, 스킵: %s", code)
            continue

        avg_price = h["avg_price"]
        highest = h.get("highest_price") or avg_price

        # 수익률 계산
        pnl_pct = ((price - avg_price) / avg_price) * 100

        # 손절 체크 (동적 파라미터)
        stop_loss = get_param("STOP_LOSS_PCT") or STOP_LOSS_PCT
        if pnl_pct <= stop_loss:
            reason = f"장중 손절 ({pnl_pct:+.1f}%, 기준: {stop_loss}%)"
            result = execute_sell(code, reason, h)
            cash += result["total_amount"]
            sold = True
            logger.info("장중 손절 매도: %s %s (%+.1f%%)", code, h["stock_name"], pnl_pct)
            continue

        # 트레일링 스탑 체크
        if price > highest:
            highest = price  # 신고가 갱신

        trailing_stop = get_param("TRAILING_STOP_PCT") or TRAILING_STOP_PCT
        trailing_pct = ((price - highest) / highest) * 100 if highest > 0 else 0
        if highest > avg_price and trailing_pct <= trailing_stop:
            reason = f"장중 트레일링 스탑 (고점 대비 {trailing_pct:+.1f}%, 기준: {trailing_stop}%)"
            result = execute_sell(code, reason, h)
            cash += result["total_amount"]
            sold = True
            logger.info("장중 트레일링 스탑: %s %s", code, h["stock_name"])
            continue

        # 현재가/최고가 갱신
        update_holding_price(code, price, highest)
        logger.debug("모니터링: %s %s 현재가=%d (매입가 대비 %+.1f%%)",
                     code, h["stock_name"], price, pnl_pct)

    # 매도가 발생했으면 cash 반영하여 일별 성과 기록
    if sold:
        _record_daily_performance(cash)
        logger.info("모니터링 매도 후 성과 기록: cash=%.0f", cash)


def sell_check():
    """장 마감 전 매도 체크 (15:20 실행) — 손절/트레일링 + 성과기록 + 리포트"""
    logger.info("=== 매도 체크 시작 (15:20) ===")

    holdings = get_holdings()
    cash = get_latest_cash()

    for h in holdings:
        code = h["stock_code"]
        price = get_realtime_price(code) or get_current_price(code)
        if price is None:
            logger.warning("현재가 조회 실패, 스킵: %s", code)
            continue

        avg_price = h["avg_price"]
        highest = h.get("highest_price") or avg_price
        pnl_pct = ((price - avg_price) / avg_price) * 100

        # 손절 체크
        stop_loss = get_param("STOP_LOSS_PCT") or STOP_LOSS_PCT
        if pnl_pct <= stop_loss:
            reason = f"손절 ({pnl_pct:+.1f}%, 기준: {stop_loss}%)"
            result = execute_sell(code, reason, h)
            cash += result["total_amount"]
            logger.info("손절 매도: %s %s (%+.1f%%)", code, h["stock_name"], pnl_pct)
            continue

        # 트레일링 스탑 체크
        if price > highest:
            highest = price
        trailing_stop = get_param("TRAILING_STOP_PCT") or TRAILING_STOP_PCT
        trailing_pct = ((price - highest) / highest) * 100 if highest > 0 else 0
        if highest > avg_price and trailing_pct <= trailing_stop:
            reason = f"트레일링 스탑 (고점 대비 {trailing_pct:+.1f}%, 기준: {trailing_stop}%)"
            result = execute_sell(code, reason, h)
            cash += result["total_amount"]
            logger.info("트레일링 스탑 매도: %s %s", code, h["stock_name"])
            continue

        # 현재가/최고가 갱신
        update_holding_price(code, price, highest)

    # 일별 성과 기록 + 텔레그램 리포트
    cash = get_latest_cash()
    _record_daily_performance(cash)
    _send_daily_report(cash)
    logger.info("=== 매도 체크 완료 ===")


def buy_orders():
    """장 시작 직후 매수 (09:05 실행) — 전일 분석 기반, 조건부 진입"""
    logger.info("=== 매수 주문 시작 (09:05) ===")

    holdings = get_holdings()
    cash = get_latest_cash()
    max_holdings = int(get_param("MAX_HOLDINGS") or MAX_HOLDINGS)
    available_slots = max_holdings - len(holdings)

    if available_slots <= 0:
        logger.info("보유 종목 가득참 (%d/%d)", len(holdings), max_holdings)
        return

    # 시장 심리 체크
    if not _check_market_sentiment():
        logger.info("시장 심리 부정적 → 신규 매수 보류")
        return

    # 일일 매수 제한
    today_buys = _get_today_buy_count()
    max_daily_buys = int(get_param("MAX_DAILY_BUYS") or 2)
    remaining_buys = max(0, max_daily_buys - today_buys)
    if remaining_buys == 0:
        logger.info("일일 매수 제한 도달 (%d/%d)", today_buys, max_daily_buys)
        return

    # 쿨다운 종목
    cooldown_codes = _get_cooldown_codes(7)
    if cooldown_codes:
        logger.info("쿨다운 종목 (%d개): %s", len(cooldown_codes), cooldown_codes)

    # 최소 현금 보유
    min_cash_ratio = get_param("MIN_CASH_RATIO") or 0.3
    min_cash = INITIAL_CAPITAL * min_cash_ratio
    available_cash = max(0, cash - min_cash)
    logger.info("매수 가능 슬롯: %d, 가용 현금: %.0f", available_slots, available_cash)

    # 전일 분석 스코어
    scores = get_company_scores()
    held_codes = {h["stock_code"] for h in holdings}
    score_threshold = get_param("SCORE_THRESHOLD") or SCORE_THRESHOLD
    buy_candidates = [
        s for s in scores
        if s["score"] >= score_threshold
           and s["stock_code"] not in held_codes
           and s["stock_code"] not in cooldown_codes
    ]

    if not buy_candidates:
        logger.info("매수 후보 없음 (스코어 >= %.2f 충족 종목 없음)", score_threshold)
        return

    for candidate in buy_candidates[:min(available_slots, remaining_buys)]:
        if available_cash < 10000:
            break

        code = candidate["stock_code"]
        name = candidate["stock_name"]

        # ── 조건부 진입: 시초가 확인 ──
        realtime = get_realtime_price(code)
        prev_close = get_current_price(code)  # 전일 종가

        if realtime is None or prev_close is None:
            logger.warning("가격 조회 실패, 매수 스킵: %s", code)
            continue

        gap_pct = ((realtime - prev_close) / prev_close) * 100

        # 갭다운 -2% 이상 → 시그널 무효
        if gap_pct <= -2.0:
            logger.info("매수 스킵 (갭다운 %.1f%%): %s %s", gap_pct, code, name)
            continue

        # 갭업 +5% 이상 → 과열, 추격매수 금지
        if gap_pct >= 5.0:
            logger.info("매수 스킵 (갭업 과열 %.1f%%): %s %s", gap_pct, code, name)
            continue

        logger.info("매수 진입: %s %s (전일대비 %+.1f%%, 스코어 %.3f)",
                    code, name, gap_pct, candidate["score"])

        result = execute_buy(code, name, candidate["score"], available_cash)
        if result:
            cash -= result["total_amount"]
            available_cash -= result["total_amount"]

    logger.info("=== 매수 주문 완료 ===")


def rebalance():
    """레거시 호환용 — sell_check + buy_orders 순차 실행"""
    sell_check()
    buy_orders()


def _check_market_sentiment() -> bool:
    """시장 심리 체크. False면 매수 보류."""
    from db import get_connection
    try:
        conn = get_connection()

        # 1) 최근 24시간 뉴스의 부정 비율 체크
        row = conn.execute(
            """SELECT
                   COUNT(*) as total,
                   COUNT(CASE WHEN sentiment = 'negative' THEN 1 END) as neg
               FROM news
               WHERE sentiment IS NOT NULL
                 AND analyzed_at >= datetime('now', 'localtime', '-1 day')"""
        ).fetchone()

        if row and row[0] >= 10:  # 뉴스 10건 이상일 때만 판단
            neg_ratio = row[1] / row[0]
            if neg_ratio > 0.6:
                logger.warning("시장 심리 경고: 부정 뉴스 비율 %.0f%% (기준: 60%%)", neg_ratio * 100)
                conn.close()
                return False

        # 2) 보유 종목 전체 손실 체크
        holdings = conn.execute(
            "SELECT avg_price, current_price FROM holdings WHERE current_price IS NOT NULL"
        ).fetchall()
        conn.close()

        if len(holdings) >= 3:  # 3종목 이상 보유 시
            all_losing = all(
                h[1] < h[0] for h in holdings if h[0] and h[1]
            )
            if all_losing:
                logger.warning("시장 심리 경고: 보유 종목 전체 손실 → 매수 보류")
                return False

        return True
    except Exception:
        logger.exception("시장 심리 체크 실패 (기본: 매수 허용)")
        return True  # 체크 실패 시 매수 허용


def _record_daily_performance(cash: float):
    """일별 성과 기록"""
    holdings = get_holdings()
    holdings_value = sum(
        (h.get("current_price") or h["avg_price"]) * h["quantity"]
        for h in holdings
    )
    total_value = cash + holdings_value
    total_return = ((total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100

    # 전일 대비 수익률
    perfs = get_daily_performances(days=2)
    if perfs:
        prev_value = perfs[0]["total_value"]
        daily_return = ((total_value - prev_value) / prev_value) * 100 if prev_value > 0 else 0
    else:
        daily_return = 0

    stats = get_win_loss_stats()
    today = datetime.now().strftime("%Y-%m-%d")

    insert_daily_performance(
        date=today,
        total_value=total_value,
        cash=cash,
        holdings_value=holdings_value,
        daily_return=round(daily_return, 2),
        total_return=round(total_return, 2),
        win_count=stats["wins"],
        loss_count=stats["losses"],
    )
    logger.info("성과 기록: 총자산=%.0f, 현금=%.0f, 주식=%.0f, 총수익률=%+.2f%%",
                total_value, cash, holdings_value, total_return)


def _send_daily_report(cash: float):
    """리밸런싱 후 텔레그램 일일 리포트 전송"""
    try:
        from notifier import notify_daily_report
        from db import get_connection

        holdings = get_holdings()
        holdings_value = sum(
            (h.get("current_price") or h["avg_price"]) * h["quantity"]
            for h in holdings
        )
        total_value = cash + holdings_value
        total_return = ((total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100

        perfs = get_daily_performances(days=2)
        daily_return = 0.0
        if perfs:
            prev = perfs[0]["total_value"]
            daily_return = ((total_value - prev) / prev) * 100 if prev > 0 else 0

        stats = get_win_loss_stats()

        # 보유종목 상세
        details = []
        for h in holdings:
            cur = h.get("current_price") or h["avg_price"]
            pnl_pct = ((cur - h["avg_price"]) / h["avg_price"]) * 100 if h["avg_price"] > 0 else 0
            details.append({"stock_name": h["stock_name"], "pnl_pct": round(pnl_pct, 1)})

        # 오늘 수집 건수
        conn = get_connection()
        row = conn.execute(
            """SELECT
                   COUNT(CASE WHEN keyword != '텔레그램' THEN 1 END) as news,
                   COUNT(CASE WHEN keyword = '텔레그램' THEN 1 END) as tg
               FROM news
               WHERE date(collected_at) = date('now', 'localtime')"""
        ).fetchone()
        conn.close()
        news_count = row[0] if row else 0
        tg_count = row[1] if row else 0

        notify_daily_report(
            total_value=total_value,
            cash=cash,
            holdings_value=holdings_value,
            total_return=round(total_return, 1),
            daily_return=round(daily_return, 1),
            holdings=details,
            wins=stats["wins"],
            losses=stats["losses"],
            news_count=news_count,
            tg_count=tg_count,
        )
    except Exception:
        logger.debug("일일 리포트 전송 실패")


def get_portfolio_summary() -> dict:
    """대시보드용 포트폴리오 요약"""
    from config import BUY_FEE_RATE, SELL_FEE_RATE, SELL_TAX_RATE

    holdings = get_holdings()
    cash = get_latest_cash()
    total_fees = get_total_fees()

    holdings_value = 0.0
    holdings_detail = []
    for h in holdings:
        current = h.get("current_price") or h["avg_price"]
        value = current * h["quantity"]
        # 수수료 반영 PnL
        buy_fee = h["avg_price"] * BUY_FEE_RATE
        sell_fee = current * (SELL_FEE_RATE + SELL_TAX_RATE)
        pnl = (current - h["avg_price"] - buy_fee - sell_fee) * h["quantity"]
        pnl_pct = pnl / (h["avg_price"] * h["quantity"]) * 100 if h["avg_price"] > 0 else 0
        holdings_value += value
        holdings_detail.append({
            **h,
            "value": round(value),
            "pnl": round(pnl),
            "pnl_pct": round(pnl_pct, 2),
        })

    total_value = cash + holdings_value
    total_return = ((total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100

    return {
        "total_value": round(total_value),
        "cash": round(cash),
        "holdings_value": round(holdings_value),
        "total_return": round(total_return, 2),
        "total_fees": round(total_fees),
        "initial_capital": INITIAL_CAPITAL,
        "holdings": holdings_detail,
        "holdings_count": len(holdings),
        "max_holdings": MAX_HOLDINGS,
    }

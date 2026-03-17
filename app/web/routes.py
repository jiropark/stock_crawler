"""웹 대시보드 라우트"""

import logging
from datetime import datetime, timedelta
from flask import Blueprint, render_template, jsonify

from strategy.portfolio import get_portfolio_summary
from strategy.scorer import get_company_scores
from strategy.learner import get_learning_dashboard_data
from db import get_trades, get_daily_performances, get_today_scores, get_win_loss_stats, get_connection

logger = logging.getLogger(__name__)

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def dashboard():
    """대시보드 (포트폴리오 현황)"""
    try:
        summary = get_portfolio_summary()
    except Exception:
        logger.exception("포트폴리오 요약 조회 실패")
        summary = {
            "total_value": 0, "cash": 0, "holdings_value": 0,
            "total_return": 0, "initial_capital": 0, "holdings": [],
            "holdings_count": 0, "max_holdings": 5,
        }
    return render_template("dashboard.html", summary=summary)


@bp.route("/news")
def news():
    """뉴스 감성분석 + 종목 스코어"""
    try:
        scores = get_company_scores()[:10]  # 상위 10개
    except Exception:
        logger.exception("스코어 조회 실패")
        scores = []
    try:
        today_news = get_today_scores()
    except Exception:
        logger.exception("오늘 뉴스 조회 실패")
        today_news = []
    return render_template("news.html", scores=scores, today_news=today_news)


@bp.route("/trades")
def trades():
    """매매 기록"""
    try:
        trade_list = get_trades(limit=100)
    except Exception:
        logger.exception("매매 기록 조회 실패")
        trade_list = []
    return render_template("trades.html", trades=trade_list)


@bp.route("/performance")
def performance():
    """AI 성과"""
    try:
        perfs = get_daily_performances(days=60)
        stats = get_win_loss_stats()
    except Exception:
        logger.exception("성과 데이터 조회 실패")
        perfs = []
        stats = {"wins": 0, "losses": 0, "total": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0}

    # 주간 AI 성적표
    weekly_stats = _get_weekly_stats()
    # AI 학습 진행도
    ai_progress = _get_ai_progress()

    # AI 학습 데이터
    try:
        learning = get_learning_dashboard_data()
    except Exception:
        logger.exception("학습 데이터 조회 실패")
        learning = {
            "current_weights": {"w_mention_freq": 0.3, "w_positive_ratio": 0.5, "w_confidence": 0.2, "score_threshold": 0.6},
            "accuracy_30d": {"total": 0, "accuracy": 0, "positive_precision": 0, "negative_precision": 0},
            "accuracy_7d": {"total": 0, "accuracy": 0, "positive_precision": 0, "negative_precision": 0},
            "weight_history": [], "metrics_history": [],
            "local_rules_active": 0, "local_rules_total": 0,
            "predictions_total": 0, "predictions_correct": 0, "predictions_evaluated": 0,
            "data_days": 0,
        }

    return render_template("performance.html", performances=perfs, stats=stats,
                           weekly_stats=weekly_stats, ai_progress=ai_progress, learning=learning)


def _get_weekly_stats() -> dict:
    """이번 주 vs 지난 주 성과 비교"""
    try:
        conn = get_connection()
        today = datetime.now()
        # 이번 주 월요일
        this_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        last_monday = (today - timedelta(days=today.weekday() + 7)).strftime("%Y-%m-%d")

        def _week_stats(start, end):
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE trade_type='SELL' AND traded_at >= ? AND traded_at < ?",
                (start, end)
            ).fetchall()
            if not rows:
                return {"win_rate": None, "avg_return": None, "count": 0}
            wins = sum(1 for r in rows if r[0] and r[0] > 0)
            total = len(rows)
            avg_pnl = sum(r[0] for r in rows if r[0]) / total if total else 0
            return {"win_rate": round(wins / total * 100, 1) if total else 0,
                    "avg_return": round(avg_pnl), "count": total}

        current = _week_stats(this_monday, today.strftime("%Y-%m-%d 23:59"))
        prev = _week_stats(last_monday, this_monday)
        conn.close()

        # 감성분석 정확도 (positive 판정 후 실제 데이터 확인은 주가 연동 후)
        return {
            "current_win_rate": current["win_rate"],
            "prev_win_rate": prev["win_rate"],
            "current_avg_return": current["avg_return"],
            "prev_avg_return": prev["avg_return"],
            "current_count": current["count"],
            "prev_count": prev["count"],
            "sentiment_accuracy": None,  # 주가 데이터 축적 후 계산
        }
    except Exception:
        logger.exception("주간 통계 조회 실패")
        return {}


def _get_ai_progress() -> dict:
    """AI 학습 진행도"""
    try:
        conn = get_connection()
        # 데이터 수집 시작일
        row = conn.execute("SELECT MIN(collected_at) FROM news").fetchone()
        conn.close()
        if row and row[0]:
            start_date = datetime.strptime(row[0][:10], "%Y-%m-%d")
            data_days = (datetime.now() - start_date).days + 1
        else:
            data_days = 0
        days_to_adjust = max(0, 30 - data_days)
        return {"data_days": data_days, "days_to_adjust": days_to_adjust}
    except Exception:
        logger.exception("AI 진행도 조회 실패")
        return {"data_days": 0, "days_to_adjust": 30}


# ── JSON API ──

@bp.route("/api/portfolio")
def api_portfolio():
    """포트폴리오 JSON"""
    try:
        return jsonify(get_portfolio_summary())
    except Exception:
        logger.exception("API 포트폴리오 오류")
        return jsonify({"error": "조회 실패"}), 500


@bp.route("/api/performance")
def api_performance():
    """성과 데이터 JSON (Chart.js용)"""
    try:
        perfs = get_daily_performances(days=60)
        # 시간순 정렬 (오래된→최신)
        perfs.reverse()
        return jsonify({
            "dates": [p["date"] for p in perfs],
            "total_values": [p["total_value"] for p in perfs],
            "daily_returns": [p["daily_return"] for p in perfs],
            "cash": [p["cash"] for p in perfs],
        })
    except Exception:
        logger.exception("API 성과 오류")
        return jsonify({"error": "조회 실패"}), 500


@bp.route("/api/scores")
def api_scores():
    """종목 스코어 JSON"""
    try:
        scores = get_company_scores()[:20]
        return jsonify(scores)
    except Exception:
        logger.exception("API 스코어 오류")
        return jsonify({"error": "조회 실패"}), 500


@bp.route("/api/learning")
def api_learning():
    """AI 학습 데이터 JSON"""
    try:
        from strategy.learner import get_learning_dashboard_data
        return jsonify(get_learning_dashboard_data())
    except Exception:
        logger.exception("API 학습 데이터 오류")
        return jsonify({"error": "조회 실패"}), 500


@bp.route("/api/stock_chart/<stock_code>")
def api_stock_chart(stock_code):
    """보유 종목 주가 차트 데이터 (최근 30일)"""
    try:
        from pykrx import stock as pykrx_stock
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
        df = pykrx_stock.get_market_ohlcv_by_date(start, end, stock_code)
        if df is None or df.empty:
            return jsonify({"error": "데이터 없음"}), 404
        # 최근 30거래일
        df = df.tail(30)
        dates = [d.strftime("%m/%d") for d in df.index]
        closes = df["종가"].tolist()

        # 매수가 조회
        conn = get_connection()
        row = conn.execute(
            "SELECT avg_price, bought_at FROM holdings WHERE stock_code=?", (stock_code,)
        ).fetchone()
        conn.close()
        avg_price = row[0] if row else None
        bought_at = row[1][:10] if row else None

        return jsonify({
            "dates": dates,
            "closes": closes,
            "avg_price": avg_price,
            "bought_at": bought_at,
        })
    except Exception:
        logger.exception("주가 차트 API 오류: %s", stock_code)
        return jsonify({"error": "조회 실패"}), 500


@bp.route("/reflections")
def reflections():
    """반성의 시간 페이지"""
    try:
        from strategy.reflection import get_reflection_dashboard_data
        data = get_reflection_dashboard_data()
    except Exception:
        logger.exception("반성 데이터 조회 실패")
        data = {"reflections": [], "current_params": {}, "tunable_params": {}}
    return render_template("reflections.html", data=data)


@bp.route("/api/reflections")
def api_reflections():
    """반성 데이터 JSON"""
    try:
        from strategy.reflection import get_reflection_dashboard_data
        return jsonify(get_reflection_dashboard_data())
    except Exception:
        logger.exception("API 반성 데이터 오류")
        return jsonify({"error": "조회 실패"}), 500

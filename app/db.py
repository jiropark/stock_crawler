"""SQLite DB 관리 모듈"""

import sqlite3
import logging
from datetime import datetime

from config import DB_PATH
from models import (
    CREATE_NEWS_TABLE, CREATE_DART_TABLE, MIGRATE_NEWS_SENTIMENT_COLUMNS,
    CREATE_HOLDINGS_TABLE, CREATE_TRADES_TABLE, CREATE_DAILY_PERFORMANCE_TABLE,
    CREATE_PREDICTION_OUTCOMES_TABLE, CREATE_AI_WEIGHT_HISTORY_TABLE,
    CREATE_LOCAL_SENTIMENT_RULES_TABLE, CREATE_AI_LEARNING_METRICS_TABLE,
    CREATE_STOCK_CODES_TABLE,
    CREATE_REFLECTION_LOG_TABLE, CREATE_DYNAMIC_CONFIG_TABLE,
)

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    """DB 커넥션 반환 (WAL 모드 사용)"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """테이블 초기화 (없으면 생성)"""
    conn = get_connection()
    try:
        conn.execute(CREATE_NEWS_TABLE)
        conn.execute(CREATE_DART_TABLE)
        conn.execute(CREATE_HOLDINGS_TABLE)
        conn.execute(CREATE_TRADES_TABLE)
        conn.execute(CREATE_DAILY_PERFORMANCE_TABLE)
        conn.execute(CREATE_PREDICTION_OUTCOMES_TABLE)
        conn.execute(CREATE_AI_WEIGHT_HISTORY_TABLE)
        conn.execute(CREATE_LOCAL_SENTIMENT_RULES_TABLE)
        conn.execute(CREATE_AI_LEARNING_METRICS_TABLE)
        conn.execute(CREATE_STOCK_CODES_TABLE)
        conn.execute(CREATE_REFLECTION_LOG_TABLE)
        conn.execute(CREATE_DYNAMIC_CONFIG_TABLE)
        conn.commit()
        logger.info("DB 초기화 완료: %s", DB_PATH)
    finally:
        conn.close()


def migrate_db():
    """기존 테이블에 새 컬럼이 없으면 ALTER TABLE로 추가 (마이그레이션)"""
    conn = get_connection()
    try:
        # 현재 news 테이블 컬럼 목록 조회
        cursor = conn.execute("PRAGMA table_info(news);")
        existing_columns = {row[1] for row in cursor.fetchall()}

        for sql in MIGRATE_NEWS_SENTIMENT_COLUMNS:
            # SQL에서 컬럼명 추출 (ALTER TABLE news ADD COLUMN <col_name> ...)
            col_name = sql.split("ADD COLUMN ")[1].split()[0]
            if col_name not in existing_columns:
                try:
                    conn.execute(sql)
                    logger.info("마이그레이션: news 테이블에 '%s' 컬럼 추가", col_name)
                except sqlite3.OperationalError as e:
                    logger.warning("마이그레이션 실패 (%s): %s", col_name, e)

        conn.commit()
        logger.info("DB 마이그레이션 완료")
    finally:
        conn.close()


def get_unanalyzed_news(limit: int = 10) -> list[dict]:
    """감성 분석이 안 된 뉴스(sentiment IS NULL) 조회"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT id, title, description FROM news WHERE sentiment IS NULL ORDER BY id LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def update_news_sentiment(news_id: int, sentiment: str, confidence: float, companies: str, summary: str):
    """뉴스 감성 분석 결과 업데이트"""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE news
               SET sentiment = ?, confidence = ?, mentioned_companies = ?,
                   ai_summary = ?, analyzed_at = ?
               WHERE id = ?""",
            (sentiment, confidence, companies, summary, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), news_id),
        )
        conn.commit()
    finally:
        conn.close()


def reset_error_news() -> int:
    """sentiment='error'인 뉴스를 미분석 상태로 리셋. 리셋 건수 반환."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """UPDATE news
               SET sentiment = NULL, confidence = NULL, mentioned_companies = NULL,
                   ai_summary = NULL, analyzed_at = NULL
               WHERE sentiment = 'error'"""
        )
        conn.commit()
        count = cursor.rowcount
        logger.info("error 뉴스 %d건 리셋 완료", count)
        return count
    finally:
        conn.close()


def insert_news(rows: list[dict]) -> int:
    """뉴스 데이터 삽입 (중복 무시). 삽입 건수 반환."""
    if not rows:
        return 0

    conn = get_connection()
    inserted = 0
    try:
        for row in rows:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO news
                   (title, link, description, source, pub_date, keyword)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    row["title"],
                    row["link"],
                    row["description"],
                    row["source"],
                    row["pub_date"],
                    row["keyword"],
                ),
            )
            if conn.total_changes > before:
                inserted += 1
        conn.commit()
    finally:
        conn.close()

    return inserted


# ── 투자 엔진 쿼리 함수 ──


def get_holdings() -> list[dict]:
    """보유 종목 전체 조회"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM holdings ORDER BY bought_at DESC")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def upsert_holding(stock_code: str, stock_name: str, quantity: int,
                   avg_price: float, current_price: float, highest_price: float,
                   bought_at: str):
    """보유 종목 삽입/갱신 (stock_code 기준 UPSERT)"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO holdings (stock_code, stock_name, quantity, avg_price,
                                     current_price, highest_price, bought_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(stock_code) DO UPDATE SET
                   quantity = excluded.quantity,
                   avg_price = excluded.avg_price,
                   current_price = excluded.current_price,
                   highest_price = excluded.highest_price,
                   updated_at = excluded.updated_at""",
            (stock_code, stock_name, quantity, avg_price, current_price,
             highest_price, bought_at, now),
        )
        conn.commit()
    finally:
        conn.close()


def update_holding_price(stock_code: str, current_price: float, highest_price: float):
    """보유 종목 현재가/최고가 갱신"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE holdings SET current_price=?, highest_price=?, updated_at=? WHERE stock_code=?",
            (current_price, highest_price, now, stock_code),
        )
        conn.commit()
    finally:
        conn.close()


def delete_holding(stock_code: str):
    """보유 종목 삭제"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM holdings WHERE stock_code=?", (stock_code,))
        conn.commit()
    finally:
        conn.close()


def insert_trade(stock_code: str, stock_name: str, trade_type: str,
                 quantity: int, price: float, total_amount: float,
                 reason: str = None, score: float = None, pnl: float = None):
    """매매 기록 삽입"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO trades
               (stock_code, stock_name, trade_type, quantity, price, total_amount,
                reason, score, pnl, traded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stock_code, stock_name, trade_type, quantity, price,
             total_amount, reason, score, pnl, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_trades(limit: int = 50) -> list[dict]:
    """매매 기록 조회 (최신순)"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM trades ORDER BY traded_at DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_latest_cash() -> float:
    """현금 잔고 계산 (trades 기반: 초기자본 - 매수총액 + 매도총액 - 수수료)"""
    from config import INITIAL_CAPITAL, BUY_FEE_RATE, SELL_FEE_RATE, SELL_TAX_RATE
    conn = get_connection()
    try:
        buy_total = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM trades WHERE trade_type = 'BUY'"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM trades WHERE trade_type = 'SELL'"
        ).fetchone()[0]
        buy_fees = int(buy_total * BUY_FEE_RATE)
        sell_fees = int(sell_total * (SELL_FEE_RATE + SELL_TAX_RATE))
        return INITIAL_CAPITAL - buy_total + sell_total - buy_fees - sell_fees
    finally:
        conn.close()


def get_total_fees() -> float:
    """누적 수수료+세금 총액"""
    from config import BUY_FEE_RATE, SELL_FEE_RATE, SELL_TAX_RATE
    conn = get_connection()
    try:
        buy_total = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM trades WHERE trade_type = 'BUY'"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COALESCE(SUM(total_amount), 0) FROM trades WHERE trade_type = 'SELL'"
        ).fetchone()[0]
        return int(buy_total * BUY_FEE_RATE) + int(sell_total * (SELL_FEE_RATE + SELL_TAX_RATE))
    finally:
        conn.close()


def insert_daily_performance(date: str, total_value: float, cash: float,
                             holdings_value: float, daily_return: float,
                             total_return: float, win_count: int, loss_count: int):
    """일별 성과 기록 삽입/갱신"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO daily_performance
               (date, total_value, cash, holdings_value, daily_return,
                total_return, win_count, loss_count, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   total_value = excluded.total_value,
                   cash = excluded.cash,
                   holdings_value = excluded.holdings_value,
                   daily_return = excluded.daily_return,
                   total_return = excluded.total_return,
                   win_count = excluded.win_count,
                   loss_count = excluded.loss_count,
                   recorded_at = excluded.recorded_at""",
            (date, total_value, cash, holdings_value, daily_return,
             total_return, win_count, loss_count, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_daily_performances(days: int = 30) -> list[dict]:
    """일별 성과 조회 (최근 N일)"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM daily_performance ORDER BY date DESC LIMIT ?", (days,)
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_analyzed_news_by_company(days: int = 3) -> list[dict]:
    """최근 N일 감성분석 완료 뉴스 (종목 언급된 것만)"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT id, title, description, sentiment, confidence,
                      mentioned_companies, ai_summary, analyzed_at
               FROM news
               WHERE mentioned_companies IS NOT NULL
                 AND mentioned_companies != ''
                 AND analyzed_at >= datetime('now', 'localtime', ?)
               ORDER BY analyzed_at DESC""",
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_today_scores() -> list[dict]:
    """오늘 뉴스 분석 결과 (감성분석 완료된 것)"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT id, title, sentiment, confidence, mentioned_companies,
                      ai_summary, analyzed_at
               FROM news
               WHERE sentiment IS NOT NULL
                 AND date(analyzed_at) = date('now', 'localtime')
               ORDER BY analyzed_at DESC"""
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_win_loss_stats() -> dict:
    """승/패 통계 (매도 기록 기준)"""
    conn = get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT
                   COUNT(CASE WHEN pnl > 0 THEN 1 END) as wins,
                   COUNT(CASE WHEN pnl <= 0 THEN 1 END) as losses,
                   COUNT(*) as total,
                   COALESCE(SUM(pnl), 0) as total_pnl,
                   COALESCE(AVG(pnl), 0) as avg_pnl
               FROM trades WHERE trade_type = 'SELL'"""
        )
        row = cursor.fetchone()
        if row:
            d = dict(row)
            d["win_rate"] = (d["wins"] / d["total"] * 100) if d["total"] > 0 else 0
            return d
        return {"wins": 0, "losses": 0, "total": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0}
    finally:
        conn.close()


def insert_dart_disclosures(rows: list[dict]) -> int:
    """DART 공시 데이터 삽입 (중복 무시). 삽입 건수 반환."""
    if not rows:
        return 0

    conn = get_connection()
    inserted = 0
    try:
        for row in rows:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO dart_disclosure
                   (corp_name, corp_code, report_nm, rcept_no, rcept_dt)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    row["corp_name"],
                    row["corp_code"],
                    row["report_nm"],
                    row["rcept_no"],
                    row["rcept_dt"],
                ),
            )
            if conn.total_changes > before:
                inserted += 1
        conn.commit()
    finally:
        conn.close()

    return inserted


def refresh_stock_codes() -> int:
    """kind.krx.co.kr에서 KOSPI/KOSDAQ 종목 목록을 다운로드하여 stock_codes 테이블 갱신"""
    import pandas as pd
    import io
    import requests

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    total = 0
    try:
        for market_type, market_name in [("stockMkt", "KOSPI"), ("kosdaqMkt", "KOSDAQ")]:
            url = "http://kind.krx.co.kr/corpgeneral/corpList.do"
            params = {"method": "download", "marketType": market_type}
            resp = requests.get(url, params=params, timeout=30)
            resp.encoding = "euc-kr"
            df = pd.read_html(io.StringIO(resp.text))[0]
            for _, row in df.iterrows():
                code = str(row["종목코드"]).zfill(6)
                name = str(row["회사명"]).strip()
                conn.execute(
                    """INSERT INTO stock_codes (stock_code, stock_name, market, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(stock_code) DO UPDATE SET
                           stock_name = excluded.stock_name,
                           market = excluded.market,
                           updated_at = excluded.updated_at""",
                    (code, name, market_name, now),
                )
                total += 1
        conn.commit()
        logger.info("종목코드 캐시 갱신: %d개", total)
    except Exception:
        logger.exception("종목코드 캐시 갱신 실패")
    finally:
        conn.close()
    return total


def get_stock_code_map() -> dict:
    """stock_codes 테이블에서 종목명→종목코드 매핑 dict 반환"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT stock_name, stock_code FROM stock_codes").fetchall()
        return {name: code for name, code in rows}
    finally:
        conn.close()

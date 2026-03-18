"""
한국 주식 뉴스/공시 데이터 수집 + 투자 전략 엔진 + 웹 대시보드

- 뉴스 수집: 평일 3회 (08:30, 12:00, 15:00)
- 공시 수집: 1시간마다 (OpenDART API)
- 감성 분석: 평일 1회 15:10 (Claude Haiku, 장중 마지막 배치)
- 매도 체크: 평일 15:20 (장 마감 10분 전, 손절/트레일링/성과기록)
- 매수 주문: 평일 09:05 (시초가 매수, 전일 분석 기반)
- 웹 대시보드: Flask (포트 8080)
"""

import logging
import signal
import sys
import threading
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from db import init_db, migrate_db
from collectors.naver_news import collect_news
from collectors.dart import collect_dart
from collectors.telegram_crawler import collect_telegram  # 웹 스크래핑 (인증 불필요)
from analyzers.sentiment import analyze_sentiment
from strategy.portfolio import rebalance, sell_check, buy_orders, monitor_positions
from strategy.learner import run_learning_cycle
from strategy.reflection import run_reflection
from strategy.scorer import reload_ticker_cache
from web.app import create_app
from config import FLASK_PORT

# ── 로깅 설정 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/logs/crawler.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def run_news():
    """뉴스 수집 래퍼 (예외 방어)"""
    try:
        logger.info("── 뉴스 수집 시작 ──")
        collect_news()
    except Exception:
        logger.exception("뉴스 수집 중 예외 발생")


def run_dart():
    """공시 수집 래퍼 (예외 방어)"""
    try:
        logger.info("── DART 공시 수집 시작 ──")
        collect_dart()
    except Exception:
        logger.exception("DART 공시 수집 중 예외 발생")


def run_telegram():
    """텔레그램 채널 수집 래퍼 (예외 방어)"""
    try:
        logger.info("── 텔레그램 채널 수집 시작 ──")
        collect_telegram()
    except Exception:
        logger.exception("텔레그램 수집 중 예외 발생")


def run_sentiment():
    """AI 감성 분석 래퍼 (예외 방어)"""
    try:
        logger.info("── AI 감성 분석 시작 ──")
        analyze_sentiment()
    except Exception:
        logger.exception("감성 분석 중 예외 발생")


def run_refresh_stock_codes():
    """종목코드 캐시 갱신 래퍼 (예외 방어)"""
    try:
        logger.info("── 종목코드 캐시 갱신 시작 ──")
        count = reload_ticker_cache()
        logger.info("── 종목코드 캐시 갱신 완료: %d개 ──", count)
    except Exception:
        logger.exception("종목코드 캐시 갱신 중 예외 발생")


def run_sell_check():
    """장 마감 전 매도 체크 래퍼 (예외 방어)"""
    try:
        logger.info("── 장 마감 전 매도 체크 시작 ──")
        sell_check()
        logger.info("── 장 마감 전 매도 체크 완료 ──")
    except Exception:
        logger.exception("매도 체크 중 예외 발생")


def run_buy_orders():
    """장 시작 매수 래퍼 (예외 방어)"""
    try:
        logger.info("── 장 시작 매수 주문 시작 ──")
        buy_orders()
        logger.info("── 장 시작 매수 주문 완료 ──")
    except Exception:
        logger.exception("매수 주문 중 예외 발생")


def run_daily_reflection():
    """일일 반성 래퍼 (예외 방어)"""
    try:
        logger.info("── 반성의 시간 시작 ──")
        run_reflection()
    except Exception:
        logger.exception("반성 중 예외 발생")


def run_monitor_positions():
    """장중 포지션 모니터링 래퍼 (예외 방어)"""
    try:
        logger.info("── 포지션 모니터링 시작 ──")
        monitor_positions()
    except Exception:
        logger.exception("포지션 모니터링 중 예외 발생")


def start_flask():
    """Flask 웹 대시보드를 daemon 스레드로 실행"""
    app = create_app()
    logger.info("Flask 대시보드 시작 (port=%d)", FLASK_PORT)
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


def main():
    logger.info("=== 주식 뉴스/공시 크롤러 시작 ===")

    # DB 초기화 + 마이그레이션
    init_db()
    migrate_db()

    # Flask 웹 대시보드 (daemon 스레드)
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # 시작 시 즉시 1회 실행
    logger.info("초기 수집 실행...")
    run_refresh_stock_codes()
    run_news()
    run_dart()
    run_telegram()
    run_sentiment()

    # 스케줄러 설정
    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # 뉴스: 평일 3회 (08:30, 12:00, 15:00)
    scheduler.add_job(
        run_news,
        CronTrigger(
            day_of_week="mon-fri",
            hour="8",
            minute="30",
            timezone="Asia/Seoul",
        ),
        id="news_collector_morning",
        name="네이버 뉴스 수집 (장 전)",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_news,
        CronTrigger(
            day_of_week="mon-fri",
            hour="12",
            minute="0",
            timezone="Asia/Seoul",
        ),
        id="news_collector_midday",
        name="네이버 뉴스 수집 (장중)",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_news,
        CronTrigger(
            day_of_week="mon-fri",
            hour="15",
            minute="0",
            timezone="Asia/Seoul",
        ),
        id="news_collector_closing",
        name="네이버 뉴스 수집 (장 마감 전)",
        misfire_grace_time=300,
    )

    # 공시: 평일 08:00~16:00, 1시간 간격
    scheduler.add_job(
        run_dart,
        CronTrigger(
            day_of_week="mon-fri",
            hour="8-16",
            minute="0",
            timezone="Asia/Seoul",
        ),
        id="dart_collector",
        name="DART 공시 수집",
        misfire_grace_time=600,  # 10분 유예
    )

    # 텔레그램: 평일 09:00~16:00, 1시간 간격 (뉴스 수집 후 15분 뒤)
    scheduler.add_job(
        run_telegram,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="15",
            timezone="Asia/Seoul",
        ),
        id="telegram_collector",
        name="텔레그램 채널 수집",
        misfire_grace_time=600,
    )

    # 감성 분석: 평일 15:10 (매도 체크 전에 완료)
    scheduler.add_job(
        run_sentiment,
        CronTrigger(
            day_of_week="mon-fri",
            hour="15",
            minute="10",
            timezone="Asia/Seoul",
        ),
        id="sentiment_analyzer",
        name="AI 감성 분석 (Claude)",
        misfire_grace_time=600,
    )

    # 매도 체크: 평일 15:20 (장 마감 10분 전 — 손절/트레일링 + 성과기록)
    scheduler.add_job(
        run_sell_check,
        CronTrigger(
            day_of_week="mon-fri",
            hour="15",
            minute="20",
            timezone="Asia/Seoul",
        ),
        id="sell_check",
        name="장 마감 전 매도 체크",
        misfire_grace_time=600,
    )

    # 매수 주문: 평일 09:05 (시초가 확인 후 조건부 매수)
    scheduler.add_job(
        run_buy_orders,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9",
            minute="5",
            timezone="Asia/Seoul",
        ),
        id="buy_orders",
        name="장 시작 조건부 매수",
        misfire_grace_time=300,
    )

    # 장중 포지션 모니터링: 평일 09:05~15:25, 10분 간격
    scheduler.add_job(
        run_monitor_positions,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="*/10",
            timezone="Asia/Seoul",
        ),
        id="position_monitor",
        name="장중 포지션 모니터링",
        misfire_grace_time=300,
    )

    # 종목코드 캐시 갱신: 평일 08:00
    scheduler.add_job(
        run_refresh_stock_codes,
        CronTrigger(
            day_of_week="mon-fri",
            hour="8",
            minute="0",
            timezone="Asia/Seoul",
        ),
        id="stock_codes_refresh",
        name="종목코드 캐시 갱신",
        misfire_grace_time=600,
    )

    # AI 학습 사이클: 평일 17:00 (장 마감 후 리밸런싱 이후)
    scheduler.add_job(
        run_learning_cycle,
        "cron",
        day_of_week="mon-fri",
        hour=17,
        minute=0,
        id="ai_learning",
        name="AI 학습 사이클",
    )

    # 반성의 시간: 평일 17:30 (학습 사이클 이후)
    scheduler.add_job(
        run_daily_reflection,
        "cron",
        day_of_week="mon-fri",
        hour=17,
        minute=30,
        id="daily_reflection",
        name="반성의 시간",
    )

    # 예약된 작업 로깅
    jobs = scheduler.get_jobs()
    for job in jobs:
        logger.info("등록된 작업: %s / 트리거: %s", job.name, job.trigger)

    # 종료 시그널 처리
    def shutdown(signum, frame):
        logger.info("종료 시그널 수신 (signal=%s), 스케줄러 중지...", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("스케줄러 시작 (매수 09:05, 매도 15:20, 분석 15:10)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")


if __name__ == "__main__":
    main()

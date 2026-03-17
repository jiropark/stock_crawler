"""환경변수 설정 로드"""

import os
from dotenv import load_dotenv

load_dotenv()

# 네이버 검색 API 키
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# OpenDART API 키
DART_API_KEY = os.getenv("DART_API_KEY", "")

# Gemini API 키 (레거시, 미사용)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Claude API 키 (감성 분석용)
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")

# DB 경로
DB_PATH = "/data/stock.db"

# 뉴스 검색 키워드 목록 (경제/증권 뉴스만 수집되도록 구체화)
NEWS_KEYWORDS = [
    "코스피 급등",
    "코스닥 급등",
    "종목 상한가",
    "실적 호재",
    "증시 대량매수",
    "52주 신고가",
    "코스피 하락",
    "코스닥 하락",
    "증시 전망",
]

# ── 투자 전략 설정 ──
INITIAL_CAPITAL = 5_000_000       # 초기 투자금 (500만원)
MAX_HOLDINGS = 5                  # 최대 보유 종목 수
MAX_PER_STOCK = 1_000_000         # 종목당 최대 투자 금액
STOP_LOSS_PCT = -5.0              # 손절 기준 (%)
TRAILING_STOP_PCT = -3.0          # 트레일링 스탑 (최고가 대비 %)
SCORE_THRESHOLD = 0.6             # 매수 스코어 기준선
WEIGHT_MENTION_FREQ = 0.3         # 언급 빈도 가중치
WEIGHT_POSITIVE_RATIO = 0.5       # 긍정 비율 가중치
WEIGHT_CONFIDENCE = 0.2           # 분석 신뢰도 가중치
SCORE_LOOKBACK_DAYS = 3           # 스코어 계산 시 참조할 뉴스 기간 (일)
MAX_DAILY_BUYS = 2                # 일일 매수 한도
MIN_CASH_RATIO = 0.30             # 최소 현금 보유 비율 (30%)
FLASK_PORT = 8080                 # 웹 대시보드 포트

# ── 수수료/세금 (2025년 기준) ──
BUY_FEE_RATE = 0.00015            # 매수 수수료 0.015% (온라인 증권사)
SELL_FEE_RATE = 0.00015           # 매도 수수료 0.015%
SELL_TAX_RATE = 0.0018            # 거래세+농특세 0.18%

# ── 텔레그램 봇 알림 ──
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# ── 텔레그램 채널 크롤링 (웹 스크래핑, 인증 불필요) ──
_DEFAULT_TG_CHANNELS = ",".join([
    "valjuman",               # 가치투자
    "corevalue",              # 코어밸류
    "stock_messenger",        # 주식 메신저
    "stockinvcowcow",         # 주식투자 꼬꼬
    "FastStockNews",          # 빠른 주식 뉴스
    "Desperatestudycafe",     # 절박한 공부 카페
])
TG_CHANNELS = [
    ch.strip()
    for ch in os.getenv("TG_CHANNELS", _DEFAULT_TG_CHANNELS).split(",")
    if ch.strip()
]


# ── 동적 파라미터 조회 ──
_dynamic_cache: dict = {}
_dynamic_loaded: bool = False


def load_dynamic_config():
    """DB에서 동적 파라미터를 캐시로 로드."""
    global _dynamic_cache, _dynamic_loaded
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT param_name, param_value FROM dynamic_config WHERE is_active = 1"
        ).fetchall()
        conn.close()
        _dynamic_cache = {r[0]: r[1] for r in rows}
        _dynamic_loaded = True
    except Exception:
        _dynamic_cache = {}
        _dynamic_loaded = True


def get_param(name: str):
    """동적 파라미터 조회. DB에 있으면 DB값, 없으면 config 기본값."""
    global _dynamic_loaded
    if not _dynamic_loaded:
        load_dynamic_config()

    if name in _dynamic_cache:
        return _dynamic_cache[name]

    # config.py의 전역 변수에서 기본값 반환
    return globals().get(name, None)


def refresh_dynamic_config():
    """동적 파라미터 캐시 강제 리로드."""
    global _dynamic_loaded
    _dynamic_loaded = False
    load_dynamic_config()

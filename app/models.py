"""DB 테이블 정의 (SQL DDL)"""

# ── 투자 엔진 테이블 ──

# 보유 종목 테이블
CREATE_HOLDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL UNIQUE,
    stock_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    current_price REAL,
    highest_price REAL,
    bought_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 매매 기록 테이블
CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    trade_type TEXT NOT NULL,  -- BUY/SELL
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    total_amount REAL NOT NULL,
    reason TEXT,
    score REAL,
    pnl REAL,
    traded_at TEXT NOT NULL
);
"""

# 일별 성과 테이블
CREATE_DAILY_PERFORMANCE_TABLE = """
CREATE TABLE IF NOT EXISTS daily_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    holdings_value REAL NOT NULL,
    daily_return REAL,
    total_return REAL,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    recorded_at TEXT NOT NULL
);
"""

# 뉴스 테이블: link 기준 중복 방지
CREATE_NEWS_TABLE = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    link TEXT NOT NULL UNIQUE,
    description TEXT,
    source TEXT,
    pub_date TEXT,
    keyword TEXT,
    collected_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    sentiment TEXT,
    confidence REAL,
    mentioned_companies TEXT,
    ai_summary TEXT,
    analyzed_at TEXT
);
"""

# 기존 news 테이블에 감성 분석 컬럼 추가 (마이그레이션용)
MIGRATE_NEWS_SENTIMENT_COLUMNS = [
    "ALTER TABLE news ADD COLUMN sentiment TEXT;",
    "ALTER TABLE news ADD COLUMN confidence REAL;",
    "ALTER TABLE news ADD COLUMN mentioned_companies TEXT;",
    "ALTER TABLE news ADD COLUMN ai_summary TEXT;",
    "ALTER TABLE news ADD COLUMN analyzed_at TEXT;",
]

# DART 공시 테이블: rcept_no 기준 중복 방지
CREATE_DART_TABLE = """
CREATE TABLE IF NOT EXISTS dart_disclosure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corp_name TEXT NOT NULL,
    corp_code TEXT,
    report_nm TEXT NOT NULL,
    rcept_no TEXT NOT NULL UNIQUE,
    rcept_dt TEXT,
    collected_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# ── AI 학습 시스템 테이블 ──

# 감성 예측 vs 실제 주가 변동 추적
CREATE_PREDICTION_OUTCOMES_TABLE = """
CREATE TABLE IF NOT EXISTS prediction_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    news_id INTEGER,
    predicted_sentiment TEXT NOT NULL,
    confidence REAL,
    score_at_buy REAL,
    price_at_prediction REAL,
    price_after_1d REAL,
    price_after_3d REAL,
    price_after_5d REAL,
    actual_return_1d REAL,
    actual_return_3d REAL,
    actual_return_5d REAL,
    was_correct INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    evaluated_at TEXT
);
"""

# 가중치 변경 이력
CREATE_AI_WEIGHT_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS ai_weight_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    w_mention_freq REAL NOT NULL,
    w_positive_ratio REAL NOT NULL,
    w_confidence REAL NOT NULL,
    score_threshold REAL NOT NULL,
    sentiment_accuracy REAL,
    win_rate REAL,
    avg_return REAL,
    adjustment_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# 로컬 감성 분류 규칙 (Gemini API 호출 절감용)
CREATE_LOCAL_SENTIMENT_RULES_TABLE = """
CREATE TABLE IF NOT EXISTS local_sentiment_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    pattern TEXT NOT NULL,
    predicted_sentiment TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    correct_count INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0,
    accuracy REAL DEFAULT 0.0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT,
    UNIQUE(keyword, pattern)
);
"""

# 일별 학습 메트릭 스냅샷
CREATE_AI_LEARNING_METRICS_TABLE = """
CREATE TABLE IF NOT EXISTS ai_learning_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    sentiment_accuracy REAL,
    positive_precision REAL,
    negative_precision REAL,
    score_correlation REAL,
    local_classification_rate REAL,
    gemini_calls_saved INTEGER DEFAULT 0,
    total_predictions INTEGER DEFAULT 0,
    correct_predictions INTEGER DEFAULT 0,
    weight_adjustment_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# 마이그레이션 (신규 테이블이므로 비어 있음)
MIGRATE_PREDICTION_OUTCOMES = []
MIGRATE_AI_WEIGHT_HISTORY = []
MIGRATE_LOCAL_SENTIMENT_RULES = []
MIGRATE_AI_LEARNING_METRICS = []

# 종목코드 캐시 테이블 (KRX 종목명→종목코드 매핑)
CREATE_STOCK_CODES_TABLE = """
CREATE TABLE IF NOT EXISTS stock_codes (
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    market TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code)
);
"""

# 반성 엔진 로그 테이블
CREATE_REFLECTION_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS reflection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    total_sells INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_profit REAL DEFAULT 0,
    avg_loss REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    total_pnl INTEGER DEFAULT 0,
    total_return REAL DEFAULT 0,
    current_holdings INTEGER DEFAULT 0,
    cash_ratio REAL DEFAULT 0,
    avg_holding_days REAL DEFAULT 0,
    parameter_changes TEXT,
    reasoning TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# 동적 파라미터 설정 테이블
CREATE_DYNAMIC_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS dynamic_config (
    param_name TEXT PRIMARY KEY,
    param_value REAL NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL,
    is_active INTEGER DEFAULT 1
);
"""
